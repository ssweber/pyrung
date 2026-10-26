"""Automatically generated module split."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag

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


class ForLoopInstruction(OneShotMixin, Instruction):
    """For-loop instruction.

    Executes its body N times within one scan.  The body is an ordered mix of
    bare instructions (implicit always-true body rungs) and body rungs, which
    evaluate their own contacts afresh on every iteration.  ``instructions``
    is the flat view: every body instruction in source order, body-rung
    instructions included, nested loops as single items.
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
            for item in self.body:
                if isinstance(item, Instruction):
                    item.execute(ctx, False)
                else:
                    item._execute_in_loop(ctx, parent_enabled=False)
            self._reset_oneshot_state(ctx)
            return

        if not self._should_execute(ctx):
            return

        count_value = resolve_tag_or_value_ctx(self.count, ctx)
        iterations = max(1, int(count_value))

        for i in range(iterations):
            # Keep loop index in tag space so indirect refs resolve via ctx.get_tag().
            ctx.set_tag(self.idx_tag.name, i)
            try:
                for item in self.body:
                    if isinstance(item, Instruction):
                        item.execute(ctx, True)
                    else:
                        item._execute_in_loop(ctx, parent_enabled=True)
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
        self.reset_oneshot()
        if self._oneshot:
            ctx.set_memory(self.memory_key("_oneshot"), False)

    def reset_oneshot(self) -> None:
        """Reset own oneshot state and propagate reset to captured children."""
        OneShotMixin.reset_oneshot(self)
        for instruction in self.instructions:
            reset_fn = getattr(instruction, "reset_oneshot", None)
            if callable(reset_fn):
                reset_fn()


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
