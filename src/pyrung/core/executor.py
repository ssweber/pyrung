"""Shared internal execution walker for ladder traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pyrung.core.context import ConditionView, RungId
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
ExecutionKind = Literal["rung", "branch", "subroutine", "synthetic"]


@dataclass(frozen=True)
class RungRun:
    """One exact rung occurrence from an observed scan."""

    rung_id: RungId
    rung: Rung
    kind: ExecutionKind
    caller_rung: int
    depth: int
    call_stack: tuple[str, ...]
    view: ConditionView
    enabled: bool
    writes: tuple[tuple[str, object], ...]


@dataclass
class _RungRunBuilder:
    slot: int
    rung_id: RungId
    rung: Rung
    kind: ExecutionKind
    caller_rung: int
    depth: int
    call_stack: tuple[str, ...]
    journal: dict[str, object]
    view: ConditionView | None = None


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

    def end_rung(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: ExecutionKind,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
        enabled: bool,
    ) -> None:
        """Called after a rung occurrence finishes executing."""

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

    def end_rung(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: ExecutionKind,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
        enabled: bool,
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


class ConditionViewCapture(_NoopExecutionObserver):
    """Observer that records each rung occurrence, at-entry view, and reads.

    Used by an on-demand replay (``PLC._replay_node_views_at`` /
    ``_replay_node_reads_at``) to reconstruct the exact intra-scan state each
    rung read.  ``begin_condition`` fires right after the executor resolves the
    rung's condition view onto ``ctx._condition_snapshot`` (see
    :func:`_execute_rung`), so reading it there captures the at-fire-time
    snapshot — including writes from rungs that ran earlier in the same scan,
    and *before* any rung that runs later consumes a gate the writer depended on.

    The key comes from ``ctx._current_node_id`` — the same ``RungId`` the
    node firing timeline uses for a subroutine rung — falling back to
    ``RungId(None, rung_index)`` at main scope.  Sharing that one source
    means the captured view and the recorded write can never key apart.
    Branches reuse their parent rung's view and are skipped.  Multiple
    calls of one subroutine in a scan keep the last call's view (matching
    the firing timeline's last-write semantics).

    **Data-read footprint (Crossings Tier 2).**  ``reads`` maps each node to the
    set of operand tags it actually read during ``execute``.  ``begin_instruction``
    points ``ctx._read_sink`` at the node's bucket; ``get_tag`` appends to it
    while the instruction runs (direct tags, block-sum elements, *resolved*
    indirect addresses all flow through ``get_tag``).  ``begin_rung`` closes the
    sink so condition-contact reads (resolved before ``begin_condition``) and
    inter-rung reads aren't attributed to the previous instruction.  A disabled
    branch's instruction short-circuits in ``guard_oneshot_execution`` before any
    read, so the bucket holds only the operands of the branch that fired —
    recorded ``cause()`` prefers this over the static (union) PDG footprint.
    """

    __slots__ = ("views", "reads", "_runs", "_active_runs")

    def __init__(self) -> None:
        self.views: dict[RungId, ConditionView] = {}
        self.reads: dict[RungId, set[str]] = {}
        self._runs: list[RungRun | None] = []
        self._active_runs: list[_RungRunBuilder] = []

    @property
    def runs(self) -> tuple[RungRun, ...]:
        """Rung occurrences in scan-entry order."""
        return tuple(run for run in self._runs if run is not None)

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
        # Close any open read bucket: the condition view is resolved next (before
        # begin_condition), and those contact reads — plus any inter-rung reads —
        # must not land in the previous rung's last instruction's bucket.
        ctx._read_sink = None
        rung_id = ctx._current_node_id or RungId(None, rung_index)
        slot = len(self._runs)
        self._runs.append(None)
        self._active_runs.append(
            _RungRunBuilder(
                slot=slot,
                rung_id=rung_id,
                rung=rung,
                kind=kind,
                caller_rung=rung_index,
                depth=depth,
                call_stack=call_stack,
                journal=ctx._begin_capture(),
            )
        )

    def end_rung(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: ExecutionKind,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
        enabled: bool,
    ) -> None:
        ctx._read_sink = None
        active = self._active_runs.pop()
        if active.rung is not rung:
            raise RuntimeError("observed rung scopes closed out of order")
        writes = ctx._finish_observed_capture(active.journal)
        if active.view is None:
            raise RuntimeError("observed rung finished without a condition view")
        self._runs[active.slot] = RungRun(
            rung_id=active.rung_id,
            rung=active.rung,
            kind=active.kind,
            caller_rung=active.caller_rung,
            depth=active.depth,
            call_stack=active.call_stack,
            view=active.view,
            enabled=enabled,
            writes=tuple(writes.items()),
        )

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
        view = ctx._condition_snapshot
        if view is not None:
            self._active_runs[-1].view = view
        if kind == "branch":
            return
        if view is not None:
            key = ctx._current_node_id or RungId(None, rung_index)
            self.views[key] = view

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
        # Point the read sink at this node's bucket for the duration of
        # ``instruction.execute``.  Reuses the bucket (setdefault) so multiple
        # instructions — and multiple branches — of one rung accumulate under the
        # same key, matching ``_writer_footprint``'s ``(rung_index, subroutine)``
        # identity.
        key = ctx._current_node_id or RungId(None, rung_index)
        ctx._read_sink = self.reads.setdefault(key, set())


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
    if mode == "natural" and observer is NOOP_OBSERVER:
        _execute_program_natural(program, ctx, capture_rungs=capture_rungs)
        return
    for rung_index, rung in enumerate(program.rungs):
        if capture_rungs:
            journal = ctx._begin_capture()
            try:
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
            finally:
                ctx._finish_rung_capture(rung_index, journal)
        else:
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


def execute_observed_rung(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    *,
    observer: ExecutionObserver,
    namespace: str,
) -> None:
    """Execute one synthetic node through the observer-aware rung path.

    Synthesis brackets are runtime overlays rather than members of
    ``program.rungs``.  Historical at-fire replay still needs their exact
    condition view and instruction-read footprint, so the runner supplies the
    overlay rung directly while retaining the user program as the subroutine
    resolution environment.
    """
    _execute_rung(
        program,
        ctx,
        rung_index,
        rung,
        mode="natural",
        observer=observer,
        kind="synthetic",
        depth=0,
        parent_enabled=True,
        subroutine_name=namespace,
        call_stack=(namespace,),
    )


def _execute_program_natural(
    program: Program,
    ctx: ScanContext,
    *,
    capture_rungs: bool,
) -> None:
    """Observer-free natural execution path used by ordinary interpreted scans."""
    for rung_index, rung in enumerate(program.rungs):
        if capture_rungs:
            journal = ctx._begin_capture()
            try:
                _execute_rung_natural(program, ctx, rung_index, rung)
            finally:
                ctx._finish_rung_capture(rung_index, journal)
        else:
            _execute_rung_natural(program, ctx, rung_index, rung)


def _execute_rung_natural(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    *,
    parent_enabled: bool = True,
    condition_view: ConditionView | None = None,
) -> None:
    if condition_view is None:
        condition_view = _resolve_condition_view(ctx, rung)
        enabled = rung._evaluate_conditions(condition_view)
    else:
        ctx._condition_snapshot = condition_view
        enabled = parent_enabled and rung._evaluate_local_conditions(condition_view)

    ctx._condition_snapshot = condition_view
    for item in rung._execution_items:
        if isinstance(item, Rung):
            _execute_rung_natural(
                program,
                ctx,
                rung_index,
                item,
                parent_enabled=enabled,
                condition_view=condition_view,
            )
        else:
            _execute_instruction_natural(program, ctx, rung_index, rung, item, enabled)


def _execute_instruction_natural(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    instruction: Instruction,
    enabled: bool,
) -> None:
    if isinstance(instruction, CallInstruction):
        _execute_call_natural(ctx, rung_index, instruction, enabled)
        return
    if isinstance(instruction, ForLoopInstruction):
        _execute_for_loop_natural(program, ctx, rung_index, rung, instruction, enabled)
        return
    instruction.execute(ctx, enabled)


def _execute_call_natural(
    ctx: ScanContext,
    rung_index: int,
    instruction: CallInstruction,
    enabled: bool,
) -> None:
    if not enabled:
        return

    program = instruction._program
    if instruction.subroutine_name not in program.subroutines:
        raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")

    saved_snapshot = ctx._condition_snapshot
    saved_scope_token = ctx._condition_scope_token
    ctx._condition_snapshot = None
    ctx._condition_scope_token = object()
    try:
        for sub_idx, sub_rung in enumerate(program.subroutines[instruction.subroutine_name]):
            rung_id = RungId(instruction.subroutine_name, sub_idx)
            journal, previous_node_id = ctx._begin_node_capture(rung_id)
            try:
                _execute_rung_natural(program, ctx, rung_index, sub_rung)
            finally:
                ctx._finish_node_capture(rung_id, journal, previous_node_id)
    except SubroutineReturnSignal:
        pass
    finally:
        ctx._condition_snapshot = saved_snapshot
        ctx._condition_scope_token = saved_scope_token


def _execute_for_loop_natural(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    instruction: ForLoopInstruction,
    enabled: bool,
) -> None:
    if not enabled:
        for child in instruction.instructions:
            _execute_instruction_natural(program, ctx, rung_index, rung, child, False)
        instruction._reset_oneshot_state(ctx)
        return

    if not instruction._should_execute(ctx):
        return

    count_value = resolve_tag_or_value_ctx(instruction.count, ctx)
    iterations = max(1, int(count_value))
    for i in range(iterations):
        ctx.set_tag(instruction.idx_tag.name, i)
        for child in instruction.instructions:
            _execute_instruction_natural(program, ctx, rung_index, rung, child, True)


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
        if condition_view is None or condition_view.scope_token is not ctx._condition_scope_token:
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

    try:
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
    finally:
        observer.end_rung(
            ctx,
            rung_index,
            rung,
            kind,
            depth,
            subroutine_name,
            call_stack,
            enabled,
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
        for sub_idx, sub_rung in enumerate(program.subroutines[instruction.subroutine_name]):
            # Capture each subroutine rung's own write slice under its
            # ``RungId`` so the node-level firing log can see subroutine
            # rungs (the enclosing main-rung ``capturing_rung`` scope still
            # rolls up the whole subtree for the unchanged main-rung log).
            # ``capturing_node`` also publishes ``ctx._current_node_id`` so
            # observers (e.g. ConditionViewCapture) key subroutine rungs by
            # the same ``RungId(sub, sub_idx)`` as the firing timeline.
            rung_id = RungId(instruction.subroutine_name, sub_idx)
            journal, previous_node_id = ctx._begin_node_capture(rung_id)
            try:
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
            finally:
                ctx._finish_node_capture(rung_id, journal, previous_node_id)
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
