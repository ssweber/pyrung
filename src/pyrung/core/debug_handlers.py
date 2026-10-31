"""Internal instruction-debug handlers used by PLCDebugger."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any, Protocol

from pyrung.core.instruction import (
    CallInstruction,
    ForLoopInstruction,
    Instruction,
    LoopBreakSignal,
    LoopContinueSignal,
    SubroutineReturnSignal,
    resolve_tag_or_value_ctx,
)
from pyrung.core.instruction.control import for_loop_iterations
from pyrung.core.site_memory import begin_bare_body_row

if TYPE_CHECKING:
    from pyrung.core.debugger import DebugInstructionState, PLCDebugger


class InstructionDebugHandler(Protocol):
    """Internal protocol for debugger instruction handlers."""

    def iter_steps(
        self,
        instruction_state: DebugInstructionState,
        debugger: PLCDebugger,
    ) -> Generator[Any, None, None]:
        """Yield debug steps for one instruction."""


class GenericInstructionDebugHandler:
    """Default handler for non-special instructions."""

    def iter_steps(
        self,
        instruction_state: DebugInstructionState,
        debugger: PLCDebugger,
    ) -> Generator[Any, None, None]:
        execution = instruction_state.execution
        instruction = instruction_state.instruction

        if not execution.enabled and instruction.is_inert_when_disabled():
            instruction.execute(execution.ctx, execution.enabled)
            return

        instruction_span = debugger._resolve_location(
            instruction,
            fallback=debugger._instruction_fallback_span(instruction_state.rung),
            fallback_end_to_line=True,
        )
        debug_substeps = getattr(instruction, "debug_substeps", None)
        if debug_substeps:
            for substep in debug_substeps:
                substep_span = debugger._resolve_location(
                    substep,
                    fallback=instruction_span,
                    fallback_end_to_line=True,
                )
                substep_condition_trace = debugger._instruction_substep_condition_trace(
                    runner=execution.runner,
                    substep=substep,
                    condition_view=execution.condition_view,
                    enabled=execution.enabled,
                    enabled_state=execution.enabled_state,
                    source_file=substep_span.source_file,
                    source_line=substep_span.source_line,
                )
                substep_trace = debugger._instruction_substep_trace(
                    source=substep_span,
                    enabled_state=execution.enabled_state,
                    condition_trace=substep_condition_trace,
                )
                yield debugger._emit_step(
                    execution=execution,
                    rung=instruction_state.rung,
                    kind="instruction",
                    source=substep_span,
                    trace=substep_trace,
                    instruction_kind=substep.instruction_kind,
                )
            instruction.execute(execution.ctx, execution.enabled)
            return

        yield debugger._emit_step(
            execution=execution,
            rung=instruction_state.rung,
            kind="instruction",
            source=instruction_span,
            trace=instruction_state.step_trace,
            instruction_kind=instruction.__class__.__name__,
        )
        instruction.execute(execution.ctx, execution.enabled)


class CallInstructionDebugHandler:
    """Control-flow handler for subroutine call instructions."""

    def iter_steps(
        self,
        instruction_state: DebugInstructionState,
        debugger: PLCDebugger,
    ) -> Generator[Any, None, None]:
        execution = instruction_state.execution
        instruction = instruction_state.instruction
        if not isinstance(instruction, CallInstruction):
            return

        if not execution.enabled:
            instruction.execute(execution.ctx, execution.enabled)
            return

        if instruction.subroutine_name not in instruction._program.subroutines:
            raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")

        instruction_span = debugger._resolve_location(
            instruction,
            fallback=debugger._instruction_fallback_span(instruction_state.rung),
            fallback_end_to_line=True,
        )
        yield debugger._emit_step(
            execution=execution,
            rung=instruction_state.rung,
            kind="instruction",
            source=instruction_span,
            trace=instruction_state.step_trace,
            instruction_kind=instruction.__class__.__name__,
        )

        next_stack = (*execution.call_stack, instruction.subroutine_name)
        saved_snapshot = execution.ctx._condition_snapshot
        saved_scope_token = execution.ctx._condition_scope_token
        execution.ctx._condition_snapshot = None
        execution.ctx._condition_scope_token = object()
        try:
            for sub_rung in instruction._program.subroutines[instruction.subroutine_name]:
                sub_condition_view = sub_rung._resolve_condition_view(execution.ctx)
                sub_enabled, sub_condition_traces = debugger._evaluate_conditions_with_trace(
                    execution.runner,
                    sub_rung._conditions,
                    sub_condition_view,
                )
                sub_execution = execution.with_overrides(
                    kind="subroutine",
                    depth=execution.depth + 1,
                    subroutine_name=instruction.subroutine_name,
                    call_stack=next_stack,
                    enabled=sub_enabled,
                    parent_enabled=True,
                    enabled_state=debugger._enabled_state_for(
                        kind="subroutine",
                        enabled=sub_enabled,
                        parent_enabled=True,
                    ),
                    condition_view=sub_condition_view,
                )
                yield from debugger._iter_rung_steps(
                    debugger._make_rung_state(
                        execution=sub_execution,
                        rung=sub_rung,
                        rung_condition_traces=sub_condition_traces,
                    )
                )
        except SubroutineReturnSignal:
            return
        finally:
            execution.ctx._condition_snapshot = saved_snapshot
            execution.ctx._condition_scope_token = saved_scope_token


class ForLoopInstructionDebugHandler:
    """Control-flow handler for for-loop instructions."""

    def iter_steps(
        self,
        instruction_state: DebugInstructionState,
        debugger: PLCDebugger,
    ) -> Generator[Any, None, None]:
        execution = instruction_state.execution
        instruction = instruction_state.instruction
        if not isinstance(instruction, ForLoopInstruction):
            return

        if not execution.enabled:
            instruction.execute(execution.ctx, execution.enabled)
            return

        if not instruction._should_execute(execution.ctx):
            return

        iterations = for_loop_iterations(resolve_tag_or_value_ctx(instruction.count, execution.ctx))

        ctx = execution.ctx
        child_execution = execution.with_overrides(enabled=True, enabled_state="enabled")
        for i in range(iterations):
            ctx.set_tag(instruction.idx_tag.name, i)
            try:
                for child in instruction.body:
                    if isinstance(child, Instruction):
                        row = begin_bare_body_row(ctx, child)
                        try:
                            yield from debugger._iter_instruction_steps(
                                debugger._make_instruction_state(
                                    execution=child_execution,
                                    rung=instruction_state.rung,
                                    instruction=child,
                                    step_trace=instruction_state.step_trace,
                                )
                            )
                        finally:
                            if row is not None:
                                ctx._condition_snapshot = row[0]
                        continue
                    outer_view = ctx._condition_snapshot
                    try:
                        body_view = child._new_row_view(ctx)
                        ctx._condition_snapshot = body_view
                        local = child._conditions[child._branch_condition_start :]
                        body_enabled, body_traces = debugger._evaluate_conditions_with_trace(
                            execution.runner, local, body_view
                        )
                        body_execution = execution.with_overrides(
                            kind="loop_body",
                            depth=execution.depth + 1,
                            enabled=body_enabled,
                            parent_enabled=True,
                            enabled_state=debugger._enabled_state_for(
                                kind="loop_body",
                                enabled=body_enabled,
                                parent_enabled=True,
                            ),
                            condition_view=body_view,
                        )
                        yield from debugger._iter_rung_steps(
                            debugger._make_rung_state(
                                execution=body_execution,
                                rung=child,
                                rung_condition_traces=body_traces,
                            )
                        )
                    finally:
                        ctx._condition_snapshot = outer_view
            except LoopContinueSignal:
                continue
            except LoopBreakSignal:
                break
