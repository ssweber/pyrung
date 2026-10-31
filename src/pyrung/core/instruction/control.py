"""Automatically generated module split."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from pyrung.core.site_memory import begin_bare_body_row
from pyrung.core.tag import Tag, TagType

from .base import (
    _EXECUTOR_CALL,
    _EXECUTOR_FOR_LOOP,
    _EXECUTOR_LOOP_EXIT,
    _EXECUTOR_RETURN,
    Instruction,
    LoopBreakSignal,
    LoopContinueSignal,
    OneShotMixin,
    SubroutineReturnSignal,
)
from .conversions import (
    _store_copy_value_to_tag_type,
)
from .resolvers import (
    _fn_name,
    _set_fault_out_of_range,
    resolve_tag_ctx,
    resolve_tag_or_value_ctx,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef
    from pyrung.core.rung import Rung


class FunctionCallInstruction(OneShotMixin, Instruction):
    """Stateless function call: copy-in / execute / copy-out."""

    _reads = ("_ins",)
    _writes = ("_outs",)
    _conditions = ()
    _structural_fields = ("_fn",)

    def __init__(
        self,
        fn: Callable[..., dict[str, Any]],
        *,
        ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
        outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        kwargs = {name: resolve_tag_or_value_ctx(src, ctx) for name, src in self._ins.items()}
        result = self._fn(**kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(
                f"run_function: {_fn_name(self._fn)!r} returned None but outs were declared"
            )
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(
                    f"run_function: {_fn_name(self._fn)!r} missing key {key!r}; got {sorted(result)}"
                )
            resolved = resolve_tag_ctx(target, ctx)
            ctx.set_tag(resolved.name, _store_copy_value_to_tag_type(result[key], resolved))


class EnabledFunctionCallInstruction(Instruction):
    """Always-execute function call with enabled flag."""

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False
    _reads = ("_ins",)
    _writes = ("_outs",)
    _conditions = ("_enable_condition",)
    _structural_fields = ("_fn",)

    def __init__(
        self,
        fn: Callable[..., dict[str, Any]],
        *,
        ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
        outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
        enable_condition: Condition | None,
    ):
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}
        self._enable_condition = enable_condition

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        kwargs = {name: resolve_tag_or_value_ctx(src, ctx) for name, src in self._ins.items()}
        result = self._fn(enabled, **kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(
                f"run_enabled_function: {_fn_name(self._fn)!r} returned None but outs were declared"
            )
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(
                    "run_enabled_function: "
                    f"{_fn_name(self._fn)!r} missing key {key!r}; got {sorted(result)}"
                )
            resolved = resolve_tag_ctx(target, ctx)
            ctx.set_tag(resolved.name, _store_copy_value_to_tag_type(result[key], resolved))


# What a For does with a count of 0.  CLICK reads the count as an unsigned
# 16-bit number (-1 runs 65535 iterations, measured on hardware 2026-10-29);
# whether 0 means no iteration or 65536 of them is still open.  The runtime,
# the debugger and the generated P1AM-200 code all read this one switch, so
# the next hardware round can flip it to 65536.
FOR_ZERO_COUNT_ITERATIONS = 0


def for_loop_iterations(count_value: Any) -> int:
    """How many times a For runs its body for a resolved count, as on CLICK.

    The count is read as unsigned 16-bit (``count & 0xFFFF``); a count of 0
    runs :data:`FOR_ZERO_COUNT_ITERATIONS` iterations.  A REAL count holding
    Inf or NaN runs none (pyrung's choice, not measured on CLICK, whose For
    takes an integer count; :meth:`ForLoopInstruction.iterations` also sets
    SC43 for it).
    """
    if not _finite_count(count_value):
        return 0
    iterations = int(count_value) & 0xFFFF
    return iterations if iterations else FOR_ZERO_COUNT_ITERATIONS


def _finite_count(count_value: Any) -> bool:
    return not isinstance(count_value, float) or math.isfinite(count_value)


def _count_may_be_non_finite(count: Any) -> bool:
    """Whether a For count can resolve to Inf or NaN (a REAL source)."""
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    if isinstance(count, Tag):
        return count.type == TagType.REAL
    if isinstance(count, (IndirectRef, IndirectExprRef)):
        return count.block.type == TagType.REAL
    if isinstance(count, Expression):
        return True
    return isinstance(count, float) and not math.isfinite(count)


class ForLoopInstruction(OneShotMixin, Instruction):
    """For-loop instruction.

    Executes its body N times within one scan.  The body is an ordered mix of
    bare instructions (implicit always-true body rungs) and body rungs, which
    evaluate their own contacts afresh on every iteration.  ``instructions``
    is the flat view: every body instruction in source order, body-rung
    instructions included, nested loops as single items.

    When the loop's rung is false the body is skipped, as on CLICK: its
    ``out()`` coils keep their values and its one-shots do not re-arm.  Only
    the For's own one-shot re-arms.  The count is read once, when the loop
    starts (:func:`for_loop_iterations`).
    """

    _reads = ("count",)
    _writes = ("idx_tag",)
    _conditions = ()
    _structural_fields = ()
    _executor_kind = _EXECUTOR_FOR_LOOP

    def __init__(
        self,
        count: Tag | IndirectRef | IndirectExprRef | Any,
        idx_tag: Tag,
        instructions: list[Instruction] | None = None,
        oneshot: bool = False,
        *,
        body: list[Instruction | Rung] | None = None,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.count = count
        self.idx_tag = idx_tag
        if _count_may_be_non_finite(count):
            # Only a For whose count can be Inf or NaN sets SC43: the analyses
            # (PDG, prover) see the flag written where it can be.
            self._fault_writes = ("fault.out_of_range",)
        self.body: tuple[Instruction | Rung, ...] = tuple(
            body if body is not None else (instructions or ())
        )
        flat: list[Instruction] = []
        for item in self.body:
            if isinstance(item, Instruction):
                flat.append(item)
            else:
                flat.extend(item._instructions)
        self.instructions = flat

    def iterations(self, ctx: ScanContext) -> int:
        """How many times the body runs this scan, reading the count now.

        A count that is Inf or NaN (a REAL source) runs no iteration and sets
        SC43 (``system.fault.out_of_range``), pyrung's choice.
        """
        try:
            count_value = resolve_tag_or_value_ctx(self.count, ctx)
            if _finite_count(count_value):
                return for_loop_iterations(count_value)
        except OverflowError:  # an expression count hit Inf or NaN on the way
            pass
        _set_fault_out_of_range(ctx)
        return 0

    @property
    def body_rungs(self) -> tuple[Rung, ...]:
        """The conditioned body rungs, in source order."""
        return tuple(item for item in self.body if not isinstance(item, Instruction))

    def iter_body(self) -> Iterator[tuple[Instruction, tuple[Condition, ...]]]:
        """Each direct body instruction, in ``instructions`` order, with the
        local contacts of the body rung it sits in (empty for a bare item).

        A walker that threads rung conditions down to instructions appends
        these to the loop rung's own.
        """
        for item in self.body:
            if isinstance(item, Instruction):
                yield item, ()
            else:
                local = tuple(item._conditions[item._branch_condition_start :])
                for instruction in item._instructions:
                    yield instruction, local

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            self._reset_oneshot_state(ctx)
            return

        if not self._should_execute(ctx):
            return

        iterations = self.iterations(ctx)

        for i in range(iterations):
            # Keep loop index in tag space so indirect refs resolve via ctx.get_tag().
            ctx.set_tag(self.idx_tag.name, i)
            try:
                for item in self.body:
                    if isinstance(item, Instruction):
                        row = begin_bare_body_row(ctx, item)
                        try:
                            item.execute(ctx, True)
                        finally:
                            if row is not None:
                                ctx._condition_snapshot = row[0]
                    else:
                        item._execute_in_loop(ctx)
            except LoopContinueSignal:
                continue
            except LoopBreakSignal:
                break

    def _should_execute(self, ctx: ScanContext) -> bool:
        """Return whether this loop should execute, using memory-backed oneshot state."""
        if not self._oneshot:
            return True

        key = self.memory_key("_oneshot")
        if ctx.get_memory(key, False):
            return False
        ctx.set_memory(key, True)
        return True

    def _reset_oneshot_state(self, ctx: ScanContext) -> None:
        """Re-arm the For's own one-shot (the rung is false); children keep theirs."""
        self.reset_oneshot()
        if self._oneshot:
            ctx.set_memory(self.memory_key("_oneshot"), False)


def iter_loop_body_rungs(instructions: Any) -> Iterator[Rung]:
    """Every forloop body rung among *instructions*, nested loops included."""
    for instruction in instructions:
        if isinstance(instruction, ForLoopInstruction):
            yield from instruction.body_rungs
            yield from iter_loop_body_rungs(instruction.instructions)


class CallInstruction(Instruction):
    """Call subroutine instruction.

    Executes a named subroutine when the rung is true.
    The subroutine must be defined in the same Program.
    """

    _reads = ()
    _writes = ()
    _conditions = ()
    _structural_fields = ("subroutine_name",)
    _executor_kind = _EXECUTOR_CALL

    def __init__(self, subroutine_name: str, program: Any):
        self.subroutine_name = subroutine_name
        self._program = program  # Reference to Program for subroutine lookup
        # Filled lazily by the shared executor after Program construction is
        # complete.  The tuple binds both traversal order and stable RungIds.
        self._executor_subroutine_plan: tuple[tuple[Any, Any], ...] | None = None
        self._executor_scope_token = object()

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        self._program._call_subroutine_ctx(self.subroutine_name, ctx)


class ReturnInstruction(Instruction):
    """Return from the current subroutine immediately."""

    _reads = ()
    _writes = ()
    _conditions = ()
    _structural_fields = ()
    _executor_kind = _EXECUTOR_RETURN

    def execute(self, ctx: ScanContext, enabled: bool) -> None:  # noqa: ARG002
        if not enabled:
            return
        raise SubroutineReturnSignal


class BreakInstruction(Instruction):
    """Leave the innermost forloop immediately (CLICK Break)."""

    _reads = ()
    _writes = ()
    _conditions = ()
    _structural_fields = ()
    _executor_kind = _EXECUTOR_LOOP_EXIT

    def execute(self, ctx: ScanContext, enabled: bool) -> None:  # noqa: ARG002
        if not enabled:
            return
        raise LoopBreakSignal


class ContinueInstruction(Instruction):
    """Skip the rest of the current forloop iteration (CLICK Continue)."""

    _reads = ()
    _writes = ()
    _conditions = ()
    _structural_fields = ()
    _executor_kind = _EXECUTOR_LOOP_EXIT

    def execute(self, ctx: ScanContext, enabled: bool) -> None:  # noqa: ARG002
        if not enabled:
            return
        raise LoopContinueSignal
