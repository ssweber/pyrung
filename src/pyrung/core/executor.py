"""Shared internal execution walker for ladder traversal."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pyrung.core.context import ConditionView
from pyrung.core.instruction import (
    CallInstruction,
    ForLoopInstruction,
    Instruction,
    ReturnInstruction,
    SubroutineReturnSignal,
    resolve_tag_or_value_ctx,
)
from pyrung.core.rung import Rung

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.program import Program


ExecutionMode = Literal["natural", "forced_on", "forced_off"]
ExecutionKind = Literal["rung", "branch", "subroutine"]


class ExecutionObserver(Protocol):
    """Observer hooks for execution boundaries."""

    def begin_rung(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: ExecutionKind,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called before a rung or rung-like branch is evaluated."""

    def begin_condition(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: ExecutionKind,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called before evaluating rung or branch conditions."""

    def begin_branch(
        self,
        ctx: ScanContext,
        rung_index: int,
        branch: Rung,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called before executing a branch body."""

    def begin_instruction(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        instruction: Instruction,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called before executing an instruction."""

    def begin_subroutine_call(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: CallInstruction,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called before entering an enabled subroutine call."""

    def begin_loop_iteration(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: ForLoopInstruction,
        iteration: int,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called before writing the loop index for one iteration."""


class _NoopExecutionObserver:
    def begin_rung(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: ExecutionKind,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
    ) -> None:
        pass

    def begin_condition(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: ExecutionKind,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
    ) -> None:
        pass

    def begin_branch(
        self,
        ctx: ScanContext,
        rung_index: int,
        branch: Rung,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        pass

    def begin_instruction(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        instruction: Instruction,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        pass

    def begin_subroutine_call(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: CallInstruction,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        pass

    def begin_loop_iteration(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: ForLoopInstruction,
        iteration: int,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        pass


NOOP_OBSERVER: ExecutionObserver = _NoopExecutionObserver()


def execute_program(
    program: Program,
    ctx: ScanContext,
    *,
    mode: ExecutionMode = "natural",
    observer: ExecutionObserver = NOOP_OBSERVER,
    capture_rungs: bool = False,
) -> None:
    """Execute top-level program rungs with shared traversal semantics."""
    _validate_mode(mode)
    for rung_index, rung in enumerate(program.rungs):
        capture = ctx.capturing_rung(rung_index) if capture_rungs else nullcontext()
        with capture:
            _execute_rung(
                program,
                ctx,
                rung_index,
                rung,
                mode=mode,
                observer=observer,
                kind="rung",
                depth=0,
                parent_enabled=True,
                subroutine_name=None,
                call_stack=(),
            )


def _validate_mode(mode: ExecutionMode) -> None:
    if mode not in ("natural", "forced_on", "forced_off"):
        raise ValueError(f"Unknown execution mode {mode!r}")


def _forced_enabled(mode: ExecutionMode, natural_enabled: bool) -> bool:
    if mode == "forced_on":
        return True
    if mode == "forced_off":
        return False
    return natural_enabled


def _new_condition_view(ctx: ScanContext) -> ConditionView:
    factory = getattr(ctx, "_new_condition_view", None)
    if callable(factory):
        view = factory()
        if not isinstance(view, ConditionView):
            raise TypeError("_new_condition_view() must return a ConditionView")
        return view
    return ConditionView(ctx)


def _resolve_condition_view(ctx: ScanContext, rung: Rung) -> ConditionView:
    if rung._use_prior_snapshot:
        condition_view = ctx._condition_snapshot
        if (
            condition_view is None
            or condition_view.scope_token is not ctx._condition_scope_token
        ):
            raise RuntimeError(
                "Rung.continued() used but no prior condition snapshot exists in the "
                "same execution scope. continued() cannot be used on the first rung in "
                "a program or subroutine, and cannot cross into or out of a subroutine."
            )
    else:
        condition_view = _new_condition_view(ctx)

    ctx._condition_snapshot = condition_view
    return condition_view


def _execute_rung(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    *,
    mode: ExecutionMode,
    observer: ExecutionObserver,
    kind: ExecutionKind,
    depth: int,
    parent_enabled: bool,
    subroutine_name: str | None,
    call_stack: tuple[str, ...],
    condition_view: ConditionView | None = None,
) -> None:
    observer.begin_rung(ctx, rung_index, rung, kind, depth, subroutine_name, call_stack)
    if kind == "branch":
        if condition_view is None:
            raise RuntimeError("Internal executor error: branch missing parent condition view")
        ctx._condition_snapshot = condition_view
    else:
        condition_view = _resolve_condition_view(ctx, rung)
    observer.begin_condition(ctx, rung_index, rung, kind, depth, subroutine_name, call_stack)

    if kind == "branch":
        natural_enabled = parent_enabled and rung._evaluate_local_conditions(condition_view)
        enabled = False if mode == "forced_off" else natural_enabled
    else:
        natural_enabled = rung._evaluate_conditions(condition_view)
        enabled = _forced_enabled(mode, natural_enabled)

    if kind == "branch":
        observer.begin_branch(ctx, rung_index, rung, depth, enabled, call_stack)

    _execute_rung_body(
        program,
        ctx,
        rung_index,
        rung,
        enabled,
        condition_view,
        mode=mode,
        observer=observer,
        depth=depth,
        subroutine_name=subroutine_name,
        call_stack=call_stack,
    )


def _execute_rung_body(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    enabled: bool,
    condition_view: ConditionView,
    *,
    mode: ExecutionMode,
    observer: ExecutionObserver,
    depth: int,
    subroutine_name: str | None,
    call_stack: tuple[str, ...],
) -> None:
    ctx._condition_snapshot = condition_view

    for item in rung._execution_items:
        if isinstance(item, Rung):
            _execute_rung(
                program,
                ctx,
                rung_index,
                item,
                mode=mode,
                observer=observer,
                kind="branch",
                depth=depth + 1,
                parent_enabled=enabled,
                subroutine_name=subroutine_name,
                call_stack=call_stack,
                condition_view=condition_view,
            )
        else:
            _execute_instruction(
                program,
                ctx,
                rung_index,
                rung,
                item,
                enabled,
                mode=mode,
                observer=observer,
                depth=depth,
                call_stack=call_stack,
            )


def _execute_instruction(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    instruction: Instruction,
    enabled: bool,
    *,
    mode: ExecutionMode,
    observer: ExecutionObserver,
    depth: int,
    call_stack: tuple[str, ...],
) -> None:
    observer.begin_instruction(ctx, rung_index, rung, instruction, depth, enabled, call_stack)

    if isinstance(instruction, CallInstruction):
        _execute_call_instruction(
            ctx,
            rung_index,
            instruction,
            enabled,
            mode=mode,
            observer=observer,
            depth=depth,
            call_stack=call_stack,
        )
        return

    if isinstance(instruction, ForLoopInstruction):
        _execute_for_loop_instruction(
            program,
            ctx,
            rung_index,
            rung,
            instruction,
            enabled,
            mode=mode,
            observer=observer,
            depth=depth,
            call_stack=call_stack,
        )
        return

    if mode == "forced_on" and isinstance(instruction, ReturnInstruction):
        instruction.execute(ctx, False)
        return

    instruction.execute(ctx, enabled)


def _execute_call_instruction(
    ctx: ScanContext,
    rung_index: int,
    instruction: CallInstruction,
    enabled: bool,
    *,
    mode: ExecutionMode,
    observer: ExecutionObserver,
    depth: int,
    call_stack: tuple[str, ...],
) -> None:
    if not enabled:
        instruction.execute(ctx, enabled)
        return

    program = instruction._program
    if instruction.subroutine_name not in program.subroutines:
        raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")

    observer.begin_subroutine_call(ctx, rung_index, instruction, depth, call_stack)
    next_stack = (*call_stack, instruction.subroutine_name)
    saved_snapshot = ctx._condition_snapshot
    saved_scope_token = ctx._condition_scope_token
    ctx._condition_snapshot = None
    ctx._condition_scope_token = object()
    try:
        for sub_rung in program.subroutines[instruction.subroutine_name]:
            _execute_rung(
                program,
                ctx,
                rung_index,
                sub_rung,
                mode=mode,
                observer=observer,
                kind="subroutine",
                depth=depth + 1,
                parent_enabled=True,
                subroutine_name=instruction.subroutine_name,
                call_stack=next_stack,
            )
    except SubroutineReturnSignal:
        pass
    finally:
        ctx._condition_snapshot = saved_snapshot
        ctx._condition_scope_token = saved_scope_token


def _execute_for_loop_instruction(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    instruction: ForLoopInstruction,
    enabled: bool,
    *,
    mode: ExecutionMode,
    observer: ExecutionObserver,
    depth: int,
    call_stack: tuple[str, ...],
) -> None:
    if not enabled:
        for child in instruction.instructions:
            _execute_instruction(
                program,
                ctx,
                rung_index,
                rung,
                child,
                False,
                mode=mode,
                observer=observer,
                depth=depth + 1,
                call_stack=call_stack,
            )
        instruction._reset_oneshot_state(ctx)
        return

    if not instruction._should_execute(ctx):
        return

    count_value = resolve_tag_or_value_ctx(instruction.count, ctx)
    iterations = max(1, int(count_value))

    for i in range(iterations):
        observer.begin_loop_iteration(ctx, rung_index, instruction, i, depth, call_stack)
        ctx.set_tag(instruction.idx_tag.name, i)
        for child in instruction.instructions:
            _execute_instruction(
                program,
                ctx,
                rung_index,
                rung,
                child,
                True,
                mode=mode,
                observer=observer,
                depth=depth + 1,
                call_stack=call_stack,
            )
