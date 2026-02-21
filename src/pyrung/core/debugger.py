"""Debugger stepping engine for PLCRunner."""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.debug_trace import (
    ConditionStatus,
    ConditionTrace,
    EnabledState,
    SourceSpan,
    TraceEvent,
    TraceRegion,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.debugger_protocol import DebugRunner
    from pyrung.core.instruction import Instruction
    from pyrung.core.rung import Rung


RungKind = Literal["rung", "branch", "subroutine"]
StepKind = Literal["rung", "branch", "subroutine", "instruction"]


@dataclass(frozen=True)
class DebugExecutionState:
    """Shared execution state passed across rung/instruction stepping."""

    runner: DebugRunner
    rung_index: int
    ctx: ScanContext
    kind: RungKind
    depth: int
    subroutine_name: str | None
    call_stack: tuple[str, ...]
    enabled: bool
    parent_enabled: bool
    enabled_state: EnabledState

    def with_overrides(self, **changes: Any) -> DebugExecutionState:
        """Return a copied state with selected fields replaced."""
        return replace(self, **changes)


@dataclass(frozen=True)
class _BranchTraceState:
    enabled_state: EnabledState
    conditions: list[ConditionTrace]


@dataclass(frozen=True)
class DebugRungState:
    """Per-rung state wrapper used by recursive rung stepping."""

    execution: DebugExecutionState
    rung: Rung
    rung_condition_traces: list[ConditionTrace]


@dataclass(frozen=True)
class DebugInstructionState:
    """Per-instruction state wrapper used by instruction handlers."""

    execution: DebugExecutionState
    rung: Rung
    instruction: Instruction
    step_trace: TraceEvent


InstructionHandler = Callable[[DebugInstructionState], Generator[Any, None, None]]


class PLCDebugger:
    """Produces debug scan steps and trace payloads for a runner."""

    def __init__(self, *, step_factory: type[Any]) -> None:
        self._step_factory = step_factory
        self._instruction_handlers: dict[type[Any], InstructionHandler] = {}
        self._register_instruction_handlers()

    def _register_instruction_handlers(self) -> None:
        from pyrung.core.instruction import CallInstruction, ForLoopInstruction

        self._instruction_handlers[CallInstruction] = self._iter_call_instruction_steps
        self._instruction_handlers[ForLoopInstruction] = self._iter_forloop_instruction_steps

    def scan_steps_debug(self, runner: DebugRunner) -> Generator[Any, None, None]:
        """Execute one scan cycle and yield debug steps."""
        ctx, dt = runner.prepare_scan()

        for i, rung in enumerate(runner.iter_top_level_rungs()):
            enabled, rung_condition_traces = self._evaluate_conditions_with_trace(
                runner, rung._conditions, ctx
            )
            execution = DebugExecutionState(
                runner=runner,
                rung_index=i,
                ctx=ctx,
                kind="rung",
                depth=0,
                subroutine_name=None,
                call_stack=(),
                enabled=enabled,
                parent_enabled=True,
                enabled_state=self._enabled_state_for(
                    kind="rung",
                    enabled=enabled,
                    parent_enabled=True,
                ),
            )
            yield from self._iter_rung_steps(
                DebugRungState(
                    execution=execution,
                    rung=rung,
                    rung_condition_traces=rung_condition_traces,
                )
            )

        runner.commit_scan(ctx, dt)

    def _iter_rung_steps(self, rung_state: DebugRungState) -> Generator[Any, None, None]:
        from pyrung.core.instruction import SubroutineReturnSignal
        from pyrung.core.rung import Rung as RungClass

        execution = rung_state.execution
        rung = rung_state.rung
        branch_enable_map, branch_trace_map = self._build_branch_maps(rung_state)

        step_trace = self._build_step_trace(
            kind=execution.kind,
            rung=rung,
            enabled_state=execution.enabled_state,
            rung_condition_traces=rung_state.rung_condition_traces,
            branch_trace_map=branch_trace_map,
        )

        try:
            for item in rung._execution_items:
                if isinstance(item, RungClass):
                    branch_enabled = branch_enable_map.get(id(item), False)
                    branch_trace = branch_trace_map.get(id(item))
                    child_conditions = list(branch_trace.conditions) if branch_trace is not None else []
                    child_execution = execution.with_overrides(
                        kind="branch",
                        depth=execution.depth + 1,
                        enabled=branch_enabled,
                        parent_enabled=execution.enabled,
                        enabled_state=self._enabled_state_for(
                            kind="branch",
                            enabled=branch_enabled,
                            parent_enabled=execution.enabled,
                        ),
                    )
                    yield from self._iter_rung_steps(
                        DebugRungState(
                            execution=child_execution,
                            rung=item,
                            rung_condition_traces=child_conditions,
                        )
                    )
                else:
                    yield from self._iter_instruction_steps(
                        DebugInstructionState(
                            execution=execution,
                            rung=rung,
                            instruction=item,
                            step_trace=step_trace,
                        )
                    )
        except SubroutineReturnSignal:
            if execution.kind != "branch":
                yield self._emit_step(
                    execution=execution,
                    rung=rung,
                    kind=execution.kind,
                    source=self._span_from_rung(rung),
                    trace=step_trace,
                    instruction_kind=None,
                )
            raise

        if execution.kind != "branch" or execution.enabled:
            yield self._emit_step(
                execution=execution,
                rung=rung,
                kind=execution.kind,
                source=self._span_from_rung(rung),
                trace=step_trace,
                instruction_kind=None,
            )

    def _iter_instruction_steps(
        self,
        instruction_state: DebugInstructionState,
    ) -> Generator[Any, None, None]:
        handler = self._resolve_instruction_handler(instruction_state.instruction)
        yield from handler(instruction_state)

    def _resolve_instruction_handler(self, instruction: Instruction) -> InstructionHandler:
        for instruction_cls in type(instruction).__mro__:
            handler = self._instruction_handlers.get(instruction_cls)
            if handler is not None:
                return handler
        return self._iter_generic_instruction_steps

    def _iter_call_instruction_steps(
        self,
        instruction_state: DebugInstructionState,
    ) -> Generator[Any, None, None]:
        from pyrung.core.instruction import CallInstruction, SubroutineReturnSignal

        execution = instruction_state.execution
        instruction = instruction_state.instruction
        if not isinstance(instruction, CallInstruction):
            return

        if not execution.enabled:
            instruction.execute(execution.ctx, execution.enabled)
            return

        if instruction.subroutine_name not in instruction._program.subroutines:
            raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")

        instruction_span = self._resolve_location(
            instruction,
            fallback=self._instruction_fallback_span(instruction_state.rung),
            fallback_end_to_line=True,
        )
        yield self._emit_step(
            execution=execution,
            rung=instruction_state.rung,
            kind="instruction",
            source=instruction_span,
            trace=instruction_state.step_trace,
            instruction_kind=instruction.__class__.__name__,
        )

        next_stack = (*execution.call_stack, instruction.subroutine_name)
        try:
            for sub_rung in instruction._program.subroutines[instruction.subroutine_name]:
                sub_enabled, sub_condition_traces = self._evaluate_conditions_with_trace(
                    execution.runner,
                    sub_rung._conditions,
                    execution.ctx,
                )
                sub_execution = execution.with_overrides(
                    kind="subroutine",
                    depth=execution.depth + 1,
                    subroutine_name=instruction.subroutine_name,
                    call_stack=next_stack,
                    enabled=sub_enabled,
                    parent_enabled=True,
                    enabled_state=self._enabled_state_for(
                        kind="subroutine",
                        enabled=sub_enabled,
                        parent_enabled=True,
                    ),
                )
                yield from self._iter_rung_steps(
                    DebugRungState(
                        execution=sub_execution,
                        rung=sub_rung,
                        rung_condition_traces=sub_condition_traces,
                    )
                )
        except SubroutineReturnSignal:
            return

    def _iter_forloop_instruction_steps(
        self,
        instruction_state: DebugInstructionState,
    ) -> Generator[Any, None, None]:
        from pyrung.core.instruction import ForLoopInstruction, resolve_tag_or_value_ctx

        execution = instruction_state.execution
        instruction = instruction_state.instruction
        if not isinstance(instruction, ForLoopInstruction):
            return

        if not execution.enabled:
            instruction.execute(execution.ctx, execution.enabled)
            return

        if not instruction.should_execute(execution.enabled):
            return

        count_value = resolve_tag_or_value_ctx(instruction.count, execution.ctx)
        iterations = max(0, int(count_value))

        child_execution = execution.with_overrides(enabled=True, enabled_state="enabled")
        for i in range(iterations):
            execution.ctx.set_tag(instruction.idx_tag.name, i)
            for child in instruction.instructions:
                yield from self._iter_instruction_steps(
                    DebugInstructionState(
                        execution=child_execution,
                        rung=instruction_state.rung,
                        instruction=child,
                        step_trace=instruction_state.step_trace,
                    )
                )

    def _iter_generic_instruction_steps(
        self,
        instruction_state: DebugInstructionState,
    ) -> Generator[Any, None, None]:
        execution = instruction_state.execution
        instruction = instruction_state.instruction

        if not execution.enabled and instruction.is_inert_when_disabled():
            instruction.execute(execution.ctx, execution.enabled)
            return

        instruction_span = self._resolve_location(
            instruction,
            fallback=self._instruction_fallback_span(instruction_state.rung),
            fallback_end_to_line=True,
        )
        debug_substeps = getattr(instruction, "debug_substeps", None)
        if debug_substeps:
            for substep in debug_substeps:
                substep_span = self._resolve_location(
                    substep,
                    fallback=instruction_span,
                    fallback_end_to_line=True,
                )
                substep_condition_trace = self._instruction_substep_condition_trace(
                    runner=execution.runner,
                    substep=substep,
                    ctx=execution.ctx,
                    enabled=execution.enabled,
                    enabled_state=execution.enabled_state,
                    source_file=substep_span.source_file,
                    source_line=substep_span.source_line,
                )
                substep_trace = self._instruction_substep_trace(
                    source=substep_span,
                    enabled_state=execution.enabled_state,
                    condition_trace=substep_condition_trace,
                )
                yield self._emit_step(
                    execution=execution,
                    rung=instruction_state.rung,
                    kind="instruction",
                    source=substep_span,
                    trace=substep_trace,
                    instruction_kind=substep.instruction_kind,
                )
            instruction.execute(execution.ctx, execution.enabled)
            return

        yield self._emit_step(
            execution=execution,
            rung=instruction_state.rung,
            kind="instruction",
            source=instruction_span,
            trace=instruction_state.step_trace,
            instruction_kind=instruction.__class__.__name__,
        )
        instruction.execute(execution.ctx, execution.enabled)

    def _instruction_substep_trace(
        self,
        *,
        source: SourceSpan,
        enabled_state: EnabledState,
        condition_trace: ConditionTrace,
    ) -> TraceEvent:
        return self._make_step_trace(
            [
                self._make_region_trace(
                    kind="instruction",
                    source=SourceSpan(
                        source_file=source.source_file,
                        source_line=source.source_line,
                        end_line=source.source_line,
                    ),
                    enabled_state=enabled_state,
                    conditions=[condition_trace],
                )
            ]
        )

    def _instruction_substep_condition_trace(
        self,
        *,
        runner: DebugRunner,
        substep: Any,
        ctx: ScanContext,
        enabled: bool,
        enabled_state: EnabledState,
        source_file: str | None,
        source_line: int | None,
    ) -> ConditionTrace:
        condition = getattr(substep, "condition", None)
        expression = getattr(substep, "expression", None)
        eval_mode = getattr(substep, "eval_mode", "condition")

        if eval_mode == "enabled":
            text = str(expression or substep.instruction_kind or "Enable")
            if enabled_state == "disabled_parent":
                return self._make_condition_trace(
                    runner=runner,
                    source_file=source_file,
                    source_line=source_line,
                    expression=text,
                    status="skipped",
                    value=None,
                    details=[],
                    summary=text,
                )
            value = bool(enabled)
            status: ConditionStatus = "true" if value else "false"
            summary = f"{text}({value})"
            return self._make_condition_trace(
                runner=runner,
                source_file=source_file,
                source_line=source_line,
                expression=text,
                status=status,
                value=value,
                details=[{"name": "enabled", "value": enabled}],
                summary=summary,
            )

        if condition is None:
            text = str(expression or substep.instruction_kind or "Condition")
            return self._make_condition_trace(
                runner=runner,
                source_file=source_file,
                source_line=source_line,
                expression=text,
                status="skipped",
                value=None,
                details=[],
                summary=text,
            )

        text = str(expression or runner.condition_expression(condition))
        if enabled_state == "disabled_parent":
            return self._make_condition_trace(
                runner=runner,
                source_file=source_file,
                source_line=source_line,
                expression=text,
                status="skipped",
                value=None,
                details=[],
                summary=text,
            )

        value, details = runner.evaluate_condition_value(condition, ctx)
        status = "true" if value else "false"
        summary = runner.condition_term_text(condition, details)
        return self._make_condition_trace(
            runner=runner,
            source_file=source_file,
            source_line=source_line,
            expression=text,
            status=status,
            value=value,
            details=details,
            summary=summary,
        )

    def _enabled_state_for(
        self,
        *,
        kind: RungKind,
        enabled: bool,
        parent_enabled: bool,
    ) -> EnabledState:
        if enabled:
            return "enabled"
        if kind == "branch" and not parent_enabled:
            return "disabled_parent"
        return "disabled_local"

    def _build_step_trace(
        self,
        *,
        kind: RungKind,
        rung: Rung,
        enabled_state: EnabledState,
        rung_condition_traces: list[ConditionTrace],
        branch_trace_map: dict[int, _BranchTraceState],
    ) -> TraceEvent:
        from pyrung.core.rung import Rung as RungClass

        regions: list[TraceRegion] = [
            self._make_region_trace(
                kind="branch" if kind == "branch" else "rung",
                source=SourceSpan(
                    source_file=rung.source_file,
                    source_line=rung.source_line,
                    end_line=self._effective_region_end_line(rung),
                ),
                enabled_state=enabled_state,
                conditions=rung_condition_traces,
            )
        ]

        for item in rung._execution_items:
            if not isinstance(item, RungClass):
                continue
            branch_trace = branch_trace_map.get(id(item))
            regions.append(
                self._make_region_trace(
                    kind="branch",
                    source=SourceSpan(
                        source_file=item.source_file,
                        source_line=item.source_line,
                        end_line=self._effective_region_end_line(item),
                    ),
                    enabled_state=(
                        branch_trace.enabled_state if branch_trace is not None else "disabled_local"
                    ),
                    conditions=list(branch_trace.conditions) if branch_trace is not None else [],
                )
            )

        return self._make_step_trace(regions)

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
        runner: DebugRunner,
        conditions: list[Any],
        ctx: ScanContext,
    ) -> tuple[bool, list[ConditionTrace]]:
        if not conditions:
            return True, []

        traces: list[ConditionTrace] = []
        enabled = True
        for condition in conditions:
            if not enabled:
                traces.append(self._skipped_condition_trace(runner, condition))
                continue
            value, details = runner.evaluate_condition_value(condition, ctx)
            if not value:
                enabled = False
            expression = runner.condition_expression(condition)
            status: ConditionStatus = "true" if value else "false"
            summary = runner.condition_term_text(condition, details)
            traces.append(
                self._make_condition_trace(
                    runner=runner,
                    source_file=getattr(condition, "source_file", None),
                    source_line=getattr(condition, "source_line", None),
                    expression=expression,
                    status=status,
                    value=value,
                    details=details,
                    summary=summary,
                )
            )
        return enabled, traces

    def _skipped_condition_trace(self, runner: DebugRunner, condition: Any) -> ConditionTrace:
        expression = runner.condition_expression(condition)
        return self._make_condition_trace(
            runner=runner,
            source_file=getattr(condition, "source_file", None),
            source_line=getattr(condition, "source_line", None),
            expression=expression,
            status="skipped",
            value=None,
            details=[],
            summary=expression,
        )

    def _make_condition_trace(
        self,
        *,
        runner: DebugRunner,
        source_file: str | None,
        source_line: int | None,
        expression: str,
        status: ConditionStatus,
        value: bool | None,
        details: list[dict[str, Any]],
        summary: str,
    ) -> ConditionTrace:
        return ConditionTrace(
            source_file=source_file,
            source_line=source_line,
            expression=expression,
            status=status,
            value=value,
            details=details,
            summary=summary,
            annotation=runner.condition_annotation(
                status=status,
                expression=expression,
                summary=summary,
            ),
        )

    def _make_region_trace(
        self,
        *,
        kind: str,
        source: SourceSpan,
        enabled_state: EnabledState,
        conditions: list[ConditionTrace],
    ) -> TraceRegion:
        return TraceRegion(
            kind=kind,
            source=source,
            enabled_state=enabled_state,
            conditions=list(conditions),
        )

    def _make_step_trace(self, regions: list[TraceRegion]) -> TraceEvent:
        return TraceEvent(regions=list(regions))

    def _build_branch_maps(
        self,
        rung_state: DebugRungState,
    ) -> tuple[dict[int, bool], dict[int, _BranchTraceState]]:
        from pyrung.core.rung import Rung as RungClass

        execution = rung_state.execution
        branch_enable_map: dict[int, bool] = {}
        branch_trace_map: dict[int, _BranchTraceState] = {}
        for item in rung_state.rung._execution_items:
            if not isinstance(item, RungClass):
                continue
            local_conditions = item._conditions[item._branch_condition_start :]
            if execution.enabled:
                local_enabled, local_traces = self._evaluate_conditions_with_trace(
                    execution.runner,
                    local_conditions,
                    execution.ctx,
                )
                branch_state: EnabledState = "enabled" if local_enabled else "disabled_local"
            else:
                local_enabled = False
                local_traces = [
                    self._skipped_condition_trace(execution.runner, cond) for cond in local_conditions
                ]
                branch_state = "disabled_parent"
            branch_enable_map[id(item)] = local_enabled
            branch_trace_map[id(item)] = _BranchTraceState(
                enabled_state=branch_state,
                conditions=local_traces,
            )

        return branch_enable_map, branch_trace_map

    def _resolve_location(
        self,
        node: Any,
        fallback: SourceSpan,
        *,
        fallback_end_to_line: bool,
    ) -> SourceSpan:
        source_file = getattr(node, "source_file", None) or fallback.source_file
        source_line = getattr(node, "source_line", None)
        if source_line is None:
            source_line = fallback.source_line

        end_line = getattr(node, "end_line", None)
        if end_line is None:
            if fallback_end_to_line:
                end_line = source_line if source_line is not None else fallback.end_line
            else:
                end_line = fallback.end_line

        return SourceSpan(
            source_file=source_file,
            source_line=(int(source_line) if source_line is not None else None),
            end_line=(int(end_line) if end_line is not None else None),
        )

    def _instruction_fallback_span(self, rung: Rung) -> SourceSpan:
        return SourceSpan(
            source_file=rung.source_file,
            source_line=rung.source_line,
            end_line=rung.source_line,
        )

    def _span_from_rung(self, rung: Rung) -> SourceSpan:
        return SourceSpan(
            source_file=rung.source_file,
            source_line=rung.source_line,
            end_line=rung.end_line,
        )

    def _emit_step(
        self,
        *,
        execution: DebugExecutionState,
        rung: Rung,
        kind: StepKind,
        source: SourceSpan,
        trace: TraceEvent | None,
        instruction_kind: str | None,
    ) -> Any:
        return self._step_factory(
            rung_index=execution.rung_index,
            rung=rung,
            ctx=execution.ctx,
            kind=kind,
            subroutine_name=execution.subroutine_name,
            depth=execution.depth,
            call_stack=execution.call_stack,
            source_file=source.source_file,
            source_line=source.source_line,
            end_line=source.end_line,
            enabled_state=execution.enabled_state,
            trace=trace,
            instruction_kind=instruction_kind,
        )
