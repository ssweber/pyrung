"""Counterfactual execution at exact intrascan occurrence boundaries.

These patches are deliberately *not* PLC input patches, synthesis overlays, or
deployable program changes. They exist only on a disposable analysis context to
answer: "if this value were established at this exact consumer, would the
remaining handoff work?" A successful execution is evidence for backward
analysis; it is never itself a reachable production step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.context import ConditionView, RungId

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.executor import ConditionViewCapture, ExecutionKind
    from pyrung.core.instruction import Instruction
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung


@dataclass(frozen=True)
class OccurrenceBoundary:
    """Exact dynamic rung boundary immediately before its condition snapshot."""

    rung_id: RungId
    execution_kind: Literal["rung", "branch", "subroutine", "synthetic"]
    caller_rung: int
    call_stack: tuple[str, ...]
    depth: int
    call_invocation: int | None
    # Sibling branches in one rung share every structural field above. The
    # replay-owned occurrence order disambiguates the exact condition boundary.
    # ``None`` preserves the older structural selector for callers which do not
    # yet possess an occurrence receipt.
    run_order: int | None = None
    # Static path through nested branches in the owning rung. Unlike
    # ``run_order``, this survives a replay whose earlier control flow differs.
    # ``None`` retains the legacy execution-coordinate selector.
    branch_path: tuple[int, ...] | None = None

    def relocates(self, observed: OccurrenceBoundary) -> bool:
        """Whether ``observed`` is this boundary in another execution."""

        return (
            self.rung_id == observed.rung_id
            and self.execution_kind == observed.execution_kind
            and self.caller_rung == observed.caller_rung
            and self.call_stack == observed.call_stack
            and self.depth == observed.depth
            and self.call_invocation == observed.call_invocation
            and (
                self.branch_path == observed.branch_path
                if self.branch_path is not None
                else self.run_order is None or self.run_order == observed.run_order
            )
        )


@dataclass(frozen=True)
class CounterfactualPatch:
    """One explicitly invalid-for-production write used by analysis only."""

    dest: str
    value: Any
    guard: Any
    boundary: OccurrenceBoundary

    def __post_init__(self) -> None:
        if self.guard is None:
            raise ValueError("CounterfactualPatch.guard is required")


@dataclass(frozen=True)
class CounterfactualPatchApplication:
    """One observed application of an analysis-only patch."""

    patch: CounterfactualPatch
    run_order: int
    before: Any
    after: Any


@dataclass(frozen=True)
class CounterfactualExecutionReceipt:
    """Exact applications made during one disposable execution."""

    applications: tuple[CounterfactualPatchApplication, ...]
    encountered_candidates: tuple[tuple[CounterfactualPatch, OccurrenceBoundary], ...] = ()

    def applications_for(
        self,
        patch: CounterfactualPatch,
    ) -> tuple[CounterfactualPatchApplication, ...]:
        return tuple(item for item in self.applications if item.patch == patch)

    def applied_exactly_once(self, patch: CounterfactualPatch) -> bool:
        """Whether the requested dynamic occurrence was selected unambiguously."""

        return len(self.applications_for(patch)) == 1


@dataclass
class _CallFrame:
    instruction: object
    invocation: int
    entered: bool = False


@dataclass
class _RungFrame:
    rung: Rung
    branch_path: tuple[int, ...]


class _CounterfactualPatchObserver:
    """Apply ordered hypothetical writes through exact executor callbacks."""

    def __init__(
        self,
        patches: tuple[CounterfactualPatch, ...],
        *,
        run_order_offset: int = 0,
    ) -> None:
        self._patches = patches
        self.applications: list[CounterfactualPatchApplication] = []
        self.encountered_candidates: list[tuple[CounterfactualPatch, OccurrenceBoundary]] = []
        self._next_call_invocation = 0
        self._call_frames: list[_CallFrame] = []
        self._active_call_invocations: list[int] = []
        self._rung_frames: list[_RungFrame] = []
        self._run_order = run_order_offset

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
        del subroutine_name
        run_order = self._run_order
        self._run_order += 1
        branch_path: tuple[int, ...] = ()
        if kind == "branch":
            if not self._rung_frames:
                raise RuntimeError("counterfactual branch has no parent rung scope")
            parent = self._rung_frames[-1]
            branch_index = next(
                (
                    index
                    for index, candidate in enumerate(parent.rung._branches)
                    if candidate is rung
                ),
                None,
            )
            if branch_index is None:
                raise RuntimeError("counterfactual branch is absent from its parent rung")
            branch_path = (*parent.branch_path, branch_index)
        boundary = OccurrenceBoundary(
            rung_id=ctx._current_node_id or RungId(None, rung_index),
            execution_kind=kind,
            caller_rung=rung_index,
            call_stack=call_stack,
            depth=depth,
            call_invocation=(
                self._active_call_invocations[-1] if self._active_call_invocations else None
            ),
            run_order=run_order,
            branch_path=branch_path,
        )
        self._rung_frames.append(_RungFrame(rung, branch_path))
        matching = tuple(
            patch for patch in self._patches if self._matches_boundary(patch.boundary, boundary)
        )
        for patch in self._patches:
            if (
                patch.boundary.rung_id == boundary.rung_id
                and patch.boundary.execution_kind == boundary.execution_kind
                and len(self.encountered_candidates) < 8
            ):
                self.encountered_candidates.append((patch, boundary))
        if not matching:
            return
        view = ConditionView(ctx)
        # The hypothetical guard decides whether analysis may inject; it is
        # not a PLC rung read and must not contaminate the execution journal.
        view._read_sink = None
        active = tuple(bool(patch.guard.evaluate(view)) for patch in matching)
        for patch, enabled in zip(matching, active, strict=True):
            if enabled:
                before = ctx._get_tag_internal(patch.dest)
                ctx.set_tag(patch.dest, patch.value)
                # A branch reuses its outer rung's frozen ConditionView. Keep
                # that disposable snapshot aligned with the hypothetical write
                # so this exact branch read—and only analysis—observes it.
                condition_view = ctx._condition_snapshot
                if kind == "branch" and condition_view is not None:
                    condition_view._tags_snapshot[patch.dest] = ctx._get_tag_internal(patch.dest)
                    if (
                        condition_view._tag_source_snapshot is not None
                        and ctx._tag_write_sources is not None
                    ):
                        condition_view._tag_source_snapshot[patch.dest] = (
                            ctx._tag_write_sources.get(patch.dest)
                        )
                self.applications.append(
                    CounterfactualPatchApplication(
                        patch=patch,
                        run_order=run_order,
                        before=before,
                        after=ctx._get_tag_internal(patch.dest),
                    )
                )

    @staticmethod
    def _matches_boundary(
        requested: OccurrenceBoundary,
        observed: OccurrenceBoundary,
    ) -> bool:
        """Match one exact boundary, with legacy structural wildcarding."""

        return requested.relocates(observed)

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
        del ctx, rung_index, kind, depth, subroutine_name, call_stack, enabled
        if not self._rung_frames or self._rung_frames[-1].rung is not rung:
            raise RuntimeError("counterfactual rung scopes closed out of order")
        self._rung_frames.pop()

    def begin_condition(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def begin_branch(self, *_args: Any, **_kwargs: Any) -> None:
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
        del ctx, rung_index, rung, depth, enabled, call_stack
        from pyrung.core.instruction import CallInstruction

        if isinstance(instruction, CallInstruction):
            self._call_frames.append(_CallFrame(instruction, self._next_call_invocation))
            self._next_call_invocation += 1

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
        del ctx, rung_index, rung, depth, enabled, call_stack
        from pyrung.core.instruction import CallInstruction

        if not isinstance(instruction, CallInstruction):
            return
        if not self._call_frames or self._call_frames[-1].instruction is not instruction:
            raise RuntimeError("counterfactual call scopes closed out of order")
        frame = self._call_frames.pop()
        if frame.entered:
            if (
                not self._active_call_invocations
                or self._active_call_invocations[-1] != frame.invocation
            ):
                raise RuntimeError("counterfactual invocation scopes closed out of order")
            self._active_call_invocations.pop()

    def begin_subroutine_call(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: Instruction,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        del ctx, rung_index, depth, call_stack
        if not self._call_frames or self._call_frames[-1].instruction is not instruction:
            raise RuntimeError("counterfactual subroutine has no call instruction scope")
        frame = self._call_frames[-1]
        frame.entered = True
        self._active_call_invocations.append(frame.invocation)

    def begin_loop_iteration(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def end_loop_iteration(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def execute_counterfactual_program(
    program: Program,
    ctx: ScanContext,
    patches: tuple[CounterfactualPatch, ...],
    *,
    capture: ConditionViewCapture | None = None,
    capture_rungs: bool = True,
) -> CounterfactualExecutionReceipt:
    """Execute one program body on a caller-owned disposable scan context."""

    from pyrung.core.executor import CompositeExecutionObserver, execute_program

    hypothetical = _CounterfactualPatchObserver(
        patches,
        run_order_offset=len(capture.runs) if capture is not None else 0,
    )
    observer = (
        hypothetical if capture is None else CompositeExecutionObserver(capture, hypothetical)
    )
    execute_program(
        program,
        ctx,
        observer=observer,
        capture_rungs=capture_rungs,
    )
    return CounterfactualExecutionReceipt(
        tuple(hypothetical.applications),
        tuple(hypothetical.encountered_candidates),
    )
