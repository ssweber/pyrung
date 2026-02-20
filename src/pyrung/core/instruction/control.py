"""Automatically generated module split."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag

from .base import Instruction, OneShotMixin, SubroutineReturnSignal
from .conversions import (
    _store_copy_value_to_tag_type,
)
from .resolvers import (
    _fn_name,
    resolve_tag_ctx,
    resolve_tag_or_value_ctx,
)

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef


class FunctionCallInstruction(OneShotMixin, Instruction):
    """Stateless function call: copy-in / execute / copy-out."""

    def __init__(
        self,
        fn: Callable[..., dict[str, Any]],
        ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None,
        outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not self.should_execute(enabled):
            return
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

    def __init__(
        self,
        fn: Callable[..., dict[str, Any]],
        ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None,
        outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None,
        enable_condition: Condition | None,
    ):
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}
        self._enable_condition = enable_condition

    def always_execute(self) -> bool:
        return True

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

    def is_inert_when_disabled(self) -> bool:
        return False


class ForLoopInstruction(OneShotMixin, Instruction):
    """For-loop instruction.

    Executes a captured instruction list N times within one scan.
    """

    def __init__(
        self,
        count: Tag | IndirectRef | IndirectExprRef | Any,
        idx_tag: Tag,
        instructions: list[Instruction],
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.count = count
        self.idx_tag = idx_tag
        self.instructions = instructions

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            for instruction in self.instructions:
                instruction.execute(ctx, False)
            self.reset_oneshot()
            return

        if not self.should_execute(enabled):
            return

        count_value = resolve_tag_or_value_ctx(self.count, ctx)
        iterations = max(0, int(count_value))

        for i in range(iterations):
            # Keep loop index in tag space so indirect refs resolve via ctx.get_tag().
            ctx.set_tag(self.idx_tag.name, i)
            for instruction in self.instructions:
                instruction.execute(ctx, True)

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

    def __init__(self, subroutine_name: str, program: Any):
        self.subroutine_name = subroutine_name
        self._program = program  # Reference to Program for subroutine lookup

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        self._program.call_subroutine_ctx(self.subroutine_name, ctx)


class ReturnInstruction(Instruction):
    """Return from the current subroutine immediately."""

    def execute(self, ctx: ScanContext, enabled: bool) -> None:  # noqa: ARG002
        if not enabled:
            return
        raise SubroutineReturnSignal
