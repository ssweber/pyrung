"""Automatically generated module split."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, assert_never

from pyrung.core.storage import CHAR_HOLDS, holds_char
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
from .data_transfer import _dest_tag, _read
from .operands import (
    ConstArg,
    Dest,
    EachOperand,
    ExprArg,
    NumberOperand,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    Span,
    TagArg,
    Value,
    describe,
    held,
    parse_value,
)
from .resolvers import (
    _fn_name,
    _set_fault_address_error,
    _set_fault_out_of_range,
    reads_indexed_element,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef
    from pyrung.core.rung import Rung


def not_a_char_error(call: str, fn_name: str, key: str, target: Dest) -> tuple[str, str]:
    """Return the text before and after the value in the error for a result no Char holds.

    A function's result is known only at the scan.  The scan and the generated code both
    raise this text, as ``TypeError`` for a value that is not a str and as ``ValueError``
    for a str that is not one character.
    """
    named = describe(held(target))
    return (
        f"{call}: {fn_name!r} returned ",
        f" for {key!r}. {named[0].upper()}{named[1:]} holds {CHAR_HOLDS}. Return one "
        f"for {key!r}, or '' for a blank.",
    )


def _store_result(
    ctx: ScanContext, call: str, fn: Callable[..., Any], key: str, value: Any, target: Dest
) -> None:
    resolved = _dest_tag(target, ctx)
    if resolved.type is TagType.CHAR and not holds_char(value):
        before, after = not_a_char_error(call, _fn_name(fn), key, target)
        error = ValueError if isinstance(value, str) else TypeError
        raise error(before + repr(value) + after)
    ctx.set_tag(resolved.name, _store_copy_value_to_tag_type(value, resolved))


_OUTS = EachOperand(arg="outs")


def parse_ins(call: str, ins: Any) -> dict[Any, Value | Span]:
    """Return the ``ins`` of a function call with each value parsed.

    The parse refuses no value.  The scan reads a tag, a pointer or an expression.  It
    passes any other value, such as a constant or a range, to the function as it is.
    The parse refuses *ins* only if it is not a mapping.
    """
    if not isinstance(ins, Mapping):
        raise TypeError(f"{call} ins must be a dict, got {type(ins).__name__}")
    return {key: parse_value(value) for key, value in ins.items()}


def _read_in(value: Value | Span, ctx: ScanContext) -> Any:
    """Return what the function gets for one of its ``ins`` on this scan."""
    match value:
        case TagArg() | PointerArg() | ExprArg() | ConstArg():
            return _read(value, ctx)
        case RangeArg(span=span) | PointerRangeArg(span=span):
            # The function gets the range object.  The scan reads none of its tags.
            return span
        case _:
            assert_never(value)


class FunctionCallInstruction(OneShotMixin, Instruction):
    """Stateless function call: copy-in / execute / copy-out."""

    _reads = ("_ins",)
    _writes = ("_outs",)
    _conditions = ()
    _structural_fields = ("_fn",)
    _operands = {"_outs": _OUTS}

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
        self._ins = parse_ins("run_function()", ins or {})
        self._outs: dict[Any, Dest] = _OUTS.parse("run_function()", "outs", outs or {})

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing, because the builder parsed every operand."""

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        kwargs = {name: _read_in(src, ctx) for name, src in self._ins.items()}
        result = ctx.call_user_function(self._fn, **kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(
                f"run_function: {_fn_name(self._fn)!r} returned None. "
                "Return a dict with a key for each name in outs=."
            )
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(
                    f"run_function: {_fn_name(self._fn)!r} returned no {key!r} key. "
                    f"It returned the keys {sorted(result)}."
                )
            _store_result(ctx, "run_function", self._fn, key, result[key], target)


class EnabledFunctionCallInstruction(Instruction):
    """Always-execute function call with enabled flag."""

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False
    _reads = ("_ins",)
    _writes = ("_outs",)
    _conditions = ("_enable_condition",)
    _structural_fields = ("_fn",)
    _operands = {"_outs": _OUTS}

    def __init__(
        self,
        fn: Callable[..., dict[str, Any]],
        *,
        ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
        outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
        enable_condition: Condition | None,
    ):
        self._fn = fn
        self._ins = parse_ins("run_enabled_function()", ins or {})
        self._outs: dict[Any, Dest] = _OUTS.parse("run_enabled_function()", "outs", outs or {})
        self._enable_condition = enable_condition

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing, because the builder parsed every operand."""

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        kwargs = {name: _read_in(src, ctx) for name, src in self._ins.items()}
        result = ctx.call_user_function(self._fn, enabled, **kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(
                f"run_enabled_function: {_fn_name(self._fn)!r} returned None. "
                "Return a dict with a key for each name in outs=."
            )
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(
                    "run_enabled_function: "
                    f"{_fn_name(self._fn)!r} returned no {key!r} key. "
                    f"It returned the keys {sorted(result)}."
                )
            _store_result(ctx, "run_enabled_function", self._fn, key, result[key], target)


# This switch sets how many iterations a For runs with a count of 0.  The runtime, the
# debugger and the generated P1AM-200 code all read it.
# CLICK reads a 16-bit count as unsigned: a DS of -1 runs 65535 iterations
# (hardware 2026-10-29).  It reads a 32-bit count whole and signed.  A DD of 65536 runs
# 65536 (hardware 2026-11-05).  A DD of -1, -65535 or -2 and a CTD of -1 run none
# (hardware 2026-11-06, 2026-11-10).  It reads a constant whole, so 70000 runs 70000
# (hardware 2026-11-10).  A count of 0 runs none.
FOR_ZERO_COUNT_ITERATIONS = 0

#: The mask of a 16-bit count (DS, TD, SD) and of a 32-bit one (DD, CTD, a constant).
FOR_COUNT_MASK_16 = 0xFFFF
FOR_COUNT_MASK_32 = 0xFFFFFFFF
#: The most iterations a 32-bit count runs.  Past it, the bits are a negative count, which
#: runs none.
FOR_COUNT_MAX_32 = 0x7FFFFFFF


def for_count_mask(count: Value) -> int:
    """Return the mask that a For reads *count* through.

    A For reads a Dint register or a constant whole.  A Dint tag, a Dint block element
    (a DD, a CTD) and an integer constant are read as signed 32-bit.  Anything else
    (an Int, a Word, an expression, a REAL) is read as unsigned 16-bit.

    A count in ``immediate()`` is read as unsigned 16-bit for every tag type.
    CLICK has no immediate For count.  The CLICK export refuses one.
    """
    match count:
        case ConstArg(value=value):
            if isinstance(value, int) and not isinstance(value, bool):
                return FOR_COUNT_MASK_32
            return FOR_COUNT_MASK_16
        case TagArg(tag=tag, immediate=immediate):
            if tag.type == TagType.DINT and not immediate:
                return FOR_COUNT_MASK_32
            return FOR_COUNT_MASK_16
        case ExprArg():
            return FOR_COUNT_MASK_16
        case PointerArg(ref=ref):
            return FOR_COUNT_MASK_32 if ref.block.type == TagType.DINT else FOR_COUNT_MASK_16
        case _:
            assert_never(count)


def for_loop_iterations(count_value: Any, mask: int = FOR_COUNT_MASK_16) -> int:
    """Return how many times a For runs its body for a resolved count, as on CLICK.

    The function reads the count through *mask* (:func:`for_count_mask`).  A 16-bit
    count is read as unsigned, ``count & 0xFFFF``.  A Dint is read whole and signed, so a
    negative one runs none.  A count of 0 runs :data:`FOR_ZERO_COUNT_ITERATIONS`
    iterations.  A REAL count that holds Inf or NaN runs none.  That is pyrung's choice.
    CLICK's editor refuses a REAL count, and a pasted one stops the PLC on its watchdog.
    :meth:`ForLoopInstruction.iterations` also sets SC43 for it.
    """
    if not _finite_count(count_value):
        return 0
    iterations = int(count_value) & mask
    if mask == FOR_COUNT_MASK_32 and iterations > FOR_COUNT_MAX_32:
        return 0
    return iterations if iterations else FOR_ZERO_COUNT_ITERATIONS


def _finite_count(count_value: Any) -> bool:
    return not isinstance(count_value, float) or math.isfinite(count_value)


def _count_may_be_non_finite(count: Value) -> bool:
    """Return whether a For count can resolve to Inf or NaN, which a REAL source can."""
    match count:
        case TagArg(tag=tag):
            return tag.type == TagType.REAL
        case PointerArg(ref=ref):
            return ref.block.type == TagType.REAL
        case ExprArg():
            return True
        case ConstArg(value=value):
            return isinstance(value, float) and not math.isfinite(value)
        case _:
            assert_never(count)


def _count_reads_indexed_element(count: Value) -> bool:
    """Return whether a For count reads a block element that a pointer picks at scan time."""
    match count:
        case PointerArg():
            return True
        case ExprArg(expr=expr):
            return reads_indexed_element(expr)
        case TagArg() | ConstArg():
            return False
        case _:
            assert_never(count)


# CLICK reads a constant count as signed 32-bit, so a count outside the span runs none.
_COUNT = NumberOperand(whole=True, span=(1, FOR_COUNT_MAX_32))


class ForLoopInstruction(OneShotMixin, Instruction):
    """For-loop instruction.

    Executes its body N times within one scan.  The body is an ordered mix of
    bare instructions (implicit always-true body rungs) and body rungs.  Body rungs
    evaluate their own contacts afresh on every iteration.  ``instructions``
    is the flat view. It lists every body instruction in source order, including the
    instructions of body rungs, and it lists a nested loop as a single item.

    When the loop's rung is false, the body is skipped, as on CLICK.  Its
    ``out()`` coils keep their values and its one-shots do not re-arm.  Only
    the For's own one-shot re-arms.  The loop reads the count once, when it
    starts (:func:`for_loop_iterations`).
    """

    _reads = ("count",)
    _writes = ("idx_tag",)
    _call_operands = (("count", "count"),)
    _conditions = ()
    _structural_fields = ()
    _operands = {"count": _COUNT}
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
        self.count: Value = _COUNT.parse("forloop()", "count", count)
        self.idx_tag = idx_tag
        # Only a For whose count can be Inf or NaN sets SC43.  Only a For whose count
        # reads an indexed element can set SC44.  The analyses (PDG, static facts) then
        # see a flag written only where it can be.
        fault_writes: tuple[str, ...] = ()
        if _count_may_be_non_finite(self.count):
            fault_writes = ("fault.out_of_range",)
        if _count_reads_indexed_element(self.count):
            fault_writes = (*fault_writes, "fault.address_error")
        if fault_writes:
            self._fault_writes = fault_writes
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

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing, because the builder parsed the count."""

    def iterations(self, ctx: ScanContext) -> int:
        """Return how many times the body runs this scan, reading the count now.

        A count that is Inf or NaN, which a REAL source can give, runs no iteration and
        sets SC43 (``system.fault.out_of_range``).  That is pyrung's choice.  A count that
        reads an element below its block runs no iteration and sets SC44.
        """
        try:
            count_value = _read(self.count, ctx)
            if _finite_count(count_value):
                return for_loop_iterations(count_value, for_count_mask(self.count))
        except OverflowError:  # an expression count hit Inf or NaN on the way
            pass
        except IndexError:
            _set_fault_address_error(ctx)
            return 0
        _set_fault_out_of_range(ctx)
        return 0

    @property
    def body_rungs(self) -> tuple[Rung, ...]:
        """Return the conditioned body rungs, in source order."""
        return tuple(item for item in self.body if not isinstance(item, Instruction))

    def iter_body(self) -> Iterator[tuple[Instruction, tuple[Condition, ...]]]:
        """Yield each direct body instruction, in ``instructions`` order.

        Each instruction comes with the local contacts of the body rung it sits in.  A
        bare item has none.  A walker that threads rung conditions down to instructions
        appends these contacts to the loop rung's own.
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
                        # A bare body box is a row of its own. It reads its pins as it
                        # starts (instruction_condition_view).
                        item.execute(ctx, True)
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
        """Re-arm the For's own one-shot when the rung is false.  The children keep theirs."""
        if self._oneshot:
            ctx.set_memory(self.memory_key("_oneshot"), False)


def iter_loop_body_rungs(instructions: Any) -> Iterator[Rung]:
    """Yield every forloop body rung among *instructions*, including those of nested loops."""
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
