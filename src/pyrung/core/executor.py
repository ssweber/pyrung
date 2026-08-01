"""Shared internal execution walker for ladder traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, cast

from pyrung.core.context import (
    ConditionView,
    OccurrenceDomain,
    ReadOccurrenceOrigin,
    RungId,
)
from pyrung.core.instruction import (
    CallInstruction,
    ForLoopInstruction,
    Instruction,
    SubroutineReturnSignal,
    resolve_tag_or_value_ctx,
)
from pyrung.core.instruction.base import (
    _EXECUTOR_BRANCH,
    _EXECUTOR_CALL,
    _EXECUTOR_FOR_LOOP,
    _EXECUTOR_RETURN,
)
from pyrung.core.rung import Rung

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.program import Program


ExecutionMode = Literal["natural", "forced_on", "forced_off"]
ExecutionKind = Literal["rung", "branch", "subroutine", "synthetic"]


@dataclass(frozen=True)
class WriteOccurrence:
    """One immediate observed context write in scan execution order."""

    ordinal: int
    domain: OccurrenceDomain
    name: str
    before: object
    after: object


@dataclass(frozen=True)
class ReadOccurrence:
    """One read and the exact definition observed in scan execution order.

    ``source`` is the actual :class:`WriteOccurrence` object when the read saw
    a pending same-scan definition. Otherwise it records why no dynamic write
    owns the value: scan ``entry``, a dynamic ``resolved`` value, ``default``,
    or an unjournaled ``pending`` definition.
    """

    ordinal: int
    domain: OccurrenceDomain
    name: str
    value: object
    source: WriteOccurrence | ReadOccurrenceOrigin

    @property
    def source_ordinal(self) -> int | None:
        """The observed same-scan write identity, if one owns this read."""
        return self.source.ordinal if isinstance(self.source, WriteOccurrence) else None


def _direct_occurrences(
    body: tuple[ExecutionBodyItem, ...],
    occurrence_type: type[ReadOccurrence] | type[WriteOccurrence],
) -> tuple[Any, ...]:
    return tuple(item for item in body if isinstance(item, occurrence_type))


def _recursive_occurrences(
    body: tuple[ExecutionBodyItem, ...],
    occurrence_type: type[ReadOccurrence] | type[WriteOccurrence],
) -> tuple[Any, ...]:
    result: list[ReadOccurrence | WriteOccurrence] = []

    def _walk(items: tuple[ExecutionBodyItem, ...]) -> None:
        for item in items:
            if isinstance(item, occurrence_type):
                result.append(item)
            elif isinstance(item, (InstructionRun, LoopIterationRun, RungRun)):
                _walk(item.body)

    _walk(body)
    return tuple(result)


@dataclass(frozen=True)
class InstructionRun:
    """One instruction execution and its recursively ordered body."""

    instruction: Instruction
    enabled: bool
    depth: int
    body: tuple[ExecutionBodyItem, ...]

    @property
    def direct_read_occurrences(self) -> tuple[ReadOccurrence, ...]:
        return _direct_occurrences(self.body, ReadOccurrence)

    @property
    def direct_write_occurrences(self) -> tuple[WriteOccurrence, ...]:
        return _direct_occurrences(self.body, WriteOccurrence)

    @property
    def read_occurrences(self) -> tuple[ReadOccurrence, ...]:
        return _recursive_occurrences(self.body, ReadOccurrence)

    @property
    def write_occurrences(self) -> tuple[WriteOccurrence, ...]:
        return _recursive_occurrences(self.body, WriteOccurrence)


@dataclass(frozen=True)
class LoopIterationRun:
    """One enabled loop iteration, including its index write and children."""

    instruction: ForLoopInstruction
    iteration: int
    depth: int
    body: tuple[ExecutionBodyItem, ...]

    @property
    def direct_read_occurrences(self) -> tuple[ReadOccurrence, ...]:
        return _direct_occurrences(self.body, ReadOccurrence)

    @property
    def direct_write_occurrences(self) -> tuple[WriteOccurrence, ...]:
        return _direct_occurrences(self.body, WriteOccurrence)

    @property
    def read_occurrences(self) -> tuple[ReadOccurrence, ...]:
        return _recursive_occurrences(self.body, ReadOccurrence)

    @property
    def write_occurrences(self) -> tuple[WriteOccurrence, ...]:
        return _recursive_occurrences(self.body, WriteOccurrence)


@dataclass(frozen=True)
class RungRun:
    """One rung execution with its authoritative recursive body journal."""

    rung_id: RungId
    rung: Rung
    kind: ExecutionKind
    caller_rung: int
    depth: int
    call_stack: tuple[str, ...]
    view: ConditionView
    enabled: bool
    body: tuple[ExecutionBodyItem, ...]

    @property
    def direct_read_occurrences(self) -> tuple[ReadOccurrence, ...]:
        """Reads owned by this rung, excluding nested branch/call rungs."""
        return _rung_direct_occurrences(self.body, ReadOccurrence)

    @property
    def direct_write_occurrences(self) -> tuple[WriteOccurrence, ...]:
        """Writes owned by this rung, excluding nested branch/call rungs."""
        return _rung_direct_occurrences(self.body, WriteOccurrence)

    @property
    def read_occurrences(self) -> tuple[ReadOccurrence, ...]:
        """All reads in this rung subtree, in scan order."""
        return _recursive_occurrences(self.body, ReadOccurrence)

    @property
    def write_occurrences(self) -> tuple[WriteOccurrence, ...]:
        """All writes in this rung subtree, in scan order."""
        return _recursive_occurrences(self.body, WriteOccurrence)

    @property
    def direct_writes(self) -> tuple[tuple[str, object], ...]:
        """Final attempted tag value per direct rung-owned write."""
        return _summarize_tag_writes(self.direct_write_occurrences)

    @property
    def writes(self) -> tuple[tuple[str, object], ...]:
        """Compatibility final-per-tag summary including descendants."""
        return _summarize_tag_writes(self.write_occurrences)

    @property
    def rung_occurrences(self) -> tuple[RungRun, ...]:
        """This rung and nested rungs in execution-entry order."""
        result: list[RungRun] = [self]
        _append_nested_rungs(self.body, result)
        return tuple(result)


ExecutionBodyItem: TypeAlias = (
    ReadOccurrence | WriteOccurrence | InstructionRun | LoopIterationRun | RungRun
)


def _rung_direct_occurrences(
    body: tuple[ExecutionBodyItem, ...],
    occurrence_type: type[ReadOccurrence] | type[WriteOccurrence],
) -> tuple[Any, ...]:
    """Walk structural instruction/loop nodes but stop at nested rungs."""
    result: list[ReadOccurrence | WriteOccurrence] = []

    def _walk(items: tuple[ExecutionBodyItem, ...]) -> None:
        for item in items:
            if isinstance(item, occurrence_type):
                result.append(item)
            elif isinstance(item, RungRun):
                continue
            elif isinstance(item, (InstructionRun, LoopIterationRun)):
                _walk(item.body)

    _walk(body)
    return tuple(result)


def _summarize_tag_writes(
    occurrences: tuple[WriteOccurrence, ...],
) -> tuple[tuple[str, object], ...]:
    summary: dict[str, object] = {}
    for occurrence in occurrences:
        if occurrence.domain == "tag":
            summary[occurrence.name] = occurrence.after
    return tuple(summary.items())


def _append_nested_rungs(body: tuple[ExecutionBodyItem, ...], result: list[RungRun]) -> None:
    for item in body:
        if isinstance(item, RungRun):
            result.append(item)
            _append_nested_rungs(item.body, result)
        elif isinstance(item, (InstructionRun, LoopIterationRun)):
            _append_nested_rungs(item.body, result)


@dataclass
class _RungRunBuilder:
    slot: int
    rung_id: RungId
    rung: Rung
    kind: ExecutionKind
    caller_rung: int
    depth: int
    call_stack: tuple[str, ...]
    body: list[object | None]
    parent_body: list[object | None]
    parent_slot: int
    view: ConditionView | None = None


@dataclass
class _InstructionRunBuilder:
    instruction: Instruction
    enabled: bool
    depth: int
    body: list[object | None]
    parent_body: list[object | None]
    parent_slot: int
    rung_id: RungId


@dataclass
class _LoopIterationRunBuilder:
    instruction: ForLoopInstruction
    iteration: int
    depth: int
    body: list[object | None]
    parent_body: list[object | None]
    parent_slot: int


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

    def end_instruction(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        instruction: Instruction,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called after an instruction and all nested execution finish."""

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

    def end_loop_iteration(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: ForLoopInstruction,
        iteration: int,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        """Called after one loop iteration and all child instructions finish."""


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

    def end_instruction(
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

    def end_loop_iteration(
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
    """Selected-replay observer for a faithful recursive execution journal.

    Used by an on-demand replay (``PLC._replay_node_views_at`` /
    ``_replay_node_reads_at``) to reconstruct the exact interpreted execution
    selected by a historical query. ``body`` is authoritative: reads, writes,
    instructions, branch/call rungs, and loop iterations retain their recursive
    execution order. Event ordinals are global within this captured scan.

    ``views`` and ``reads`` remain compact compatibility projections. ``runs``
    remains a flat entry-order view over the recursive rung nodes. No ordered
    structures are allocated on ordinary scans; context callbacks are installed
    only while this observer drives the selected interpreted replay.
    """

    __slots__ = (
        "views",
        "reads",
        "_body",
        "_body_stack",
        "_runs",
        "_active_runs",
        "_active_instructions",
        "_active_iterations",
        "_ordinal",
        "_causal_projection",
    )

    def __init__(self) -> None:
        self.views: dict[RungId, ConditionView] = {}
        self.reads: dict[RungId, set[str]] = {}
        self._body: list[object | None] = []
        self._body_stack: list[list[object | None]] = []
        self._runs: list[RungRun | None] = []
        self._active_runs: list[_RungRunBuilder] = []
        self._active_instructions: list[_InstructionRunBuilder] = []
        self._active_iterations: list[_LoopIterationRunBuilder] = []
        self._ordinal = 0
        # Lazily populated by the historical causal reader.  The projection is
        # another immutable view of this exact selected-scan journal, so it has
        # the same epoch/scan lifetime as the capture itself.
        self._causal_projection: Any = None

    @property
    def body(self) -> tuple[ExecutionBodyItem, ...]:
        """Root scan journal, including events outside rung scopes."""
        return _freeze_body(self._body)

    @property
    def runs(self) -> tuple[RungRun, ...]:
        """Rung occurrences in scan-entry order."""
        return tuple(run for run in self._runs if run is not None)

    def attach(self, ctx: ScanContext) -> None:
        """Attach this selected-scan journal before any observable scan phase."""
        ctx._read_sink = self._record_read
        ctx._write_sink = self._record_write
        if ctx._tag_write_sources is None:
            ctx._tag_write_sources = {}
        if ctx._memory_write_sources is None:
            ctx._memory_write_sources = {}

    def _install_sinks(self, ctx: ScanContext) -> None:
        self.attach(ctx)

    def _event_body(self) -> list[object | None]:
        return self._body_stack[-1] if self._body_stack else self._body

    def _record_read(
        self,
        domain: OccurrenceDomain,
        name: str,
        value: object,
        origin: ReadOccurrenceOrigin,
        source: object | None,
    ) -> None:
        if source is not None and not isinstance(source, WriteOccurrence):
            raise RuntimeError("observed read received an unknown definition token")
        occurrence = ReadOccurrence(
            self._ordinal,
            domain,
            name,
            value,
            source if source is not None else origin,
        )
        self._ordinal += 1
        body = self._event_body()
        body.append(occurrence)
        if (
            domain == "tag"
            and self._active_instructions
            and body is self._active_instructions[-1].body
        ):
            self.reads.setdefault(self._active_instructions[-1].rung_id, set()).add(name)

    def _record_write(
        self,
        domain: OccurrenceDomain,
        name: str,
        before: object,
        after: object,
    ) -> WriteOccurrence:
        occurrence = WriteOccurrence(self._ordinal, domain, name, before, after)
        self._event_body().append(occurrence)
        self._ordinal += 1
        return occurrence

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
        self._install_sinks(ctx)
        rung_id = ctx._current_node_id or RungId(None, rung_index)
        flat_slot = len(self._runs)
        self._runs.append(None)
        parent_body = self._event_body()
        parent_slot = len(parent_body)
        parent_body.append(None)
        body: list[object | None] = []
        self._active_runs.append(
            _RungRunBuilder(
                slot=flat_slot,
                rung_id=rung_id,
                rung=rung,
                kind=kind,
                caller_rung=rung_index,
                depth=depth,
                call_stack=call_stack,
                body=body,
                parent_body=parent_body,
                parent_slot=parent_slot,
            )
        )
        self._body_stack.append(body)

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
        body = self._body_stack.pop()
        active = self._active_runs.pop()
        if active.rung is not rung or body is not active.body:
            raise RuntimeError("observed rung scopes closed out of order")
        if active.view is None:
            raise RuntimeError("observed rung finished without a condition view")
        active.view._read_sink = None
        run = RungRun(
            rung_id=active.rung_id,
            rung=active.rung,
            kind=active.kind,
            caller_rung=active.caller_rung,
            depth=active.depth,
            call_stack=active.call_stack,
            view=active.view,
            enabled=enabled,
            body=_freeze_body(active.body),
        )
        self._runs[active.slot] = run
        active.parent_body[active.parent_slot] = run

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
            view._read_sink = self._record_read
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
        key = ctx._current_node_id or RungId(None, rung_index)
        self.reads.setdefault(key, set())
        parent_body = self._event_body()
        parent_slot = len(parent_body)
        parent_body.append(None)
        body: list[object | None] = []
        self._active_instructions.append(
            _InstructionRunBuilder(
                instruction=instruction,
                enabled=enabled,
                depth=depth,
                body=body,
                parent_body=parent_body,
                parent_slot=parent_slot,
                rung_id=key,
            )
        )
        self._body_stack.append(body)
        self._install_sinks(ctx)

    def end_instruction(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        instruction: Instruction,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        body = self._body_stack.pop()
        active = self._active_instructions.pop()
        if active.instruction is not instruction or body is not active.body:
            raise RuntimeError("observed instruction scopes closed out of order")
        run = InstructionRun(
            instruction=instruction,
            enabled=enabled,
            depth=depth,
            body=_freeze_body(active.body),
        )
        active.parent_body[active.parent_slot] = run
        self._install_sinks(ctx)

    def begin_loop_iteration(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: ForLoopInstruction,
        iteration: int,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        parent_body = self._event_body()
        parent_slot = len(parent_body)
        parent_body.append(None)
        body: list[object | None] = []
        self._active_iterations.append(
            _LoopIterationRunBuilder(
                instruction=instruction,
                iteration=iteration,
                depth=depth,
                body=body,
                parent_body=parent_body,
                parent_slot=parent_slot,
            )
        )
        self._body_stack.append(body)
        self._install_sinks(ctx)

    def end_loop_iteration(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: ForLoopInstruction,
        iteration: int,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        body = self._body_stack.pop()
        active = self._active_iterations.pop()
        if (
            active.instruction is not instruction
            or active.iteration != iteration
            or body is not active.body
        ):
            raise RuntimeError("observed loop scopes closed out of order")
        run = LoopIterationRun(
            instruction=instruction,
            iteration=iteration,
            depth=depth,
            body=_freeze_body(active.body),
        )
        active.parent_body[active.parent_slot] = run
        self._install_sinks(ctx)


def _freeze_body(body: list[object | None]) -> tuple[ExecutionBodyItem, ...]:
    if any(item is None for item in body):
        raise RuntimeError("observed execution body contains an unclosed scope")
    return cast(tuple[ExecutionBodyItem, ...], tuple(body))


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

    for item_kind, item in rung._execution_plan:
        if item_kind == _EXECUTOR_BRANCH:
            _execute_rung_natural(
                program,
                ctx,
                rung_index,
                item,  # ty: ignore[invalid-argument-type]
                parent_enabled=enabled,
                condition_view=condition_view,
            )
        else:
            _execute_instruction_natural(
                program,
                ctx,
                rung_index,
                rung,
                item,  # ty: ignore[invalid-argument-type]
                enabled,
                item_kind=item_kind,
            )


def _execute_instruction_natural(
    program: Program,
    ctx: ScanContext,
    rung_index: int,
    rung: Rung,
    instruction: Instruction,
    enabled: bool,
    *,
    item_kind: int | None = None,
) -> None:
    if item_kind is None:
        item_kind = instruction._executor_kind
    if item_kind == _EXECUTOR_CALL:
        _execute_call_natural(
            ctx,
            rung_index,
            instruction,  # ty: ignore[invalid-argument-type]
            enabled,
        )
        return
    if item_kind == _EXECUTOR_FOR_LOOP:
        _execute_for_loop_natural(
            program,
            ctx,
            rung_index,
            rung,
            instruction,  # ty: ignore[invalid-argument-type]
            enabled,
        )
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

    subroutine_plan = _resolve_subroutine_plan(instruction)

    saved_snapshot = ctx._condition_snapshot
    saved_scope_token = ctx._condition_scope_token
    ctx._condition_snapshot = None
    ctx._condition_scope_token = instruction._executor_scope_token
    try:
        for rung_id, sub_rung in subroutine_plan:
            journal, previous_node_id = ctx._begin_node_capture(rung_id)
            try:
                _execute_rung_natural(instruction._program, ctx, rung_index, sub_rung)
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
            _execute_instruction_natural(
                program,
                ctx,
                rung_index,
                rung,
                child,
                False,
                item_kind=child._executor_kind,
            )
        instruction._reset_oneshot_state(ctx)
        return

    if not instruction._should_execute(ctx):
        return

    count_value = resolve_tag_or_value_ctx(instruction.count, ctx)
    iterations = max(1, int(count_value))
    for i in range(iterations):
        ctx.set_tag(instruction.idx_tag.name, i)
        for child in instruction.instructions:
            _execute_instruction_natural(
                program,
                ctx,
                rung_index,
                rung,
                child,
                True,
                item_kind=child._executor_kind,
            )


def _resolve_subroutine_plan(
    instruction: CallInstruction,
) -> tuple[tuple[RungId, Rung], ...]:
    """Bind immutable subroutine traversal metadata once per call site."""
    plan = instruction._executor_subroutine_plan
    if plan is not None:
        return plan
    program = instruction._program
    rungs = program.subroutines.get(instruction.subroutine_name)
    if rungs is None:
        raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")
    plan = tuple(
        (RungId(instruction.subroutine_name, sub_idx), sub_rung)
        for sub_idx, sub_rung in enumerate(rungs)
    )
    instruction._executor_subroutine_plan = plan
    return plan


def _validate_mode(mode: ExecutionMode) -> None:
    if mode not in ("natural", "forced_on", "forced_off"):
        raise ValueError(f"Unknown execution mode {mode!r}")


def _forced_enabled(mode: ExecutionMode, natural_enabled: bool) -> bool:
    if mode == "forced_on":
        return True
    if mode == "forced_off":
        return False
    return natural_enabled


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
        condition_view = ctx._new_condition_view()

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

    for item_kind, item in rung._execution_plan:
        if item_kind == _EXECUTOR_BRANCH:
            _execute_rung(
                program,
                ctx,
                rung_index,
                item,  # ty: ignore[invalid-argument-type]
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
                item,  # ty: ignore[invalid-argument-type]
                enabled,
                mode=mode,
                observer=observer,
                depth=depth,
                call_stack=call_stack,
                item_kind=item_kind,
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
    item_kind: int | None = None,
) -> None:
    observer.begin_instruction(ctx, rung_index, rung, instruction, depth, enabled, call_stack)
    try:
        if item_kind is None:
            item_kind = instruction._executor_kind
        if item_kind == _EXECUTOR_CALL:
            _execute_call_instruction(
                ctx,
                rung_index,
                instruction,  # ty: ignore[invalid-argument-type]
                enabled,
                mode=mode,
                observer=observer,
                depth=depth,
                call_stack=call_stack,
            )
            return

        if item_kind == _EXECUTOR_FOR_LOOP:
            _execute_for_loop_instruction(
                program,
                ctx,
                rung_index,
                rung,
                instruction,  # ty: ignore[invalid-argument-type]
                enabled,
                mode=mode,
                observer=observer,
                depth=depth,
                call_stack=call_stack,
            )
            return

        if mode == "forced_on" and item_kind == _EXECUTOR_RETURN:
            instruction.execute(ctx, False)
            return

        instruction.execute(ctx, enabled)
    finally:
        observer.end_instruction(
            ctx,
            rung_index,
            rung,
            instruction,
            depth,
            enabled,
            call_stack,
        )


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
    subroutine_plan = _resolve_subroutine_plan(instruction)

    observer.begin_subroutine_call(ctx, rung_index, instruction, depth, call_stack)
    next_stack = (*call_stack, instruction.subroutine_name)
    saved_snapshot = ctx._condition_snapshot
    saved_scope_token = ctx._condition_scope_token
    ctx._condition_snapshot = None
    ctx._condition_scope_token = instruction._executor_scope_token
    try:
        for rung_id, sub_rung in subroutine_plan:
            # Capture each subroutine rung's own write slice under its
            # ``RungId`` so the node-level firing log can see subroutine
            # rungs (the enclosing main-rung ``capturing_rung`` scope still
            # rolls up the whole subtree for the unchanged main-rung log).
            # ``capturing_node`` also publishes ``ctx._current_node_id`` so
            # observers (e.g. ConditionViewCapture) key subroutine rungs by
            # the same ``RungId(sub, sub_idx)`` as the firing timeline.
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
                item_kind=child._executor_kind,
            )
        instruction._reset_oneshot_state(ctx)
        return

    if not instruction._should_execute(ctx):
        return

    count_value = resolve_tag_or_value_ctx(instruction.count, ctx)
    iterations = max(1, int(count_value))

    for i in range(iterations):
        observer.begin_loop_iteration(ctx, rung_index, instruction, i, depth, call_stack)
        try:
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
                    item_kind=child._executor_kind,
                )
        finally:
            observer.end_loop_iteration(
                ctx,
                rung_index,
                instruction,
                i,
                depth,
                call_stack,
            )
