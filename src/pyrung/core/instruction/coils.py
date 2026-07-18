"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core.tag import ImmediateRef, Tag, TagType

from .base import Instruction, OneShotMixin
from .resolvers import (
    resolve_coil_targets_ctx,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange


_RESET_VALUES: dict[TagType, bool | int | float | str] = {
    TagType.BOOL: False,
    TagType.INT: 0,
    TagType.DINT: 0,
    TagType.REAL: 0.0,
    TagType.WORD: 0,
    TagType.CHAR: "",
}


def reset_value_for_type(tag_type: TagType) -> bool | int | float | str:
    """Return the OFF/zero value written by a RESET instruction."""
    return _RESET_VALUES[tag_type]


class OutInstruction(OneShotMixin, Instruction):
    """Output coil instruction (OUT).

    Sets the target bit to True when executed.
    """

    INERT_WHEN_DISABLED = False
    _reads = ()
    _writes = ("target",)
    _conditions = ()
    _structural_fields = ()

    def __init__(
        self,
        target: Tag | BlockRange | IndirectBlockRange | ImmediateRef,
        *,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.target = target

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        targets = resolve_coil_targets_ctx(self.target, ctx)
        if not enabled:
            self.reset_oneshot()
            if self._oneshot:
                ctx.set_memory(self.memory_key("_oneshot"), False)
            for target in targets:
                ctx.set_tag(target.name, False)
            return

        if self._oneshot:
            key = self.memory_key("_oneshot")
            if ctx.get_memory(key, False):
                for target in targets:
                    ctx.set_tag(target.name, False)
                return
            ctx.set_memory(key, True)
        elif not self.should_execute(enabled):
            for target in targets:
                ctx.set_tag(target.name, False)
            return
        for target in targets:
            ctx.set_tag(target.name, True)


class LatchInstruction(Instruction):
    """Latch/Set instruction (SET).

    Sets the target bit to True. Unlike OUT, this is typically
    not reset when the rung goes false.
    """

    _reads = ()
    _writes = ("target",)
    _conditions = ()
    _structural_fields = ()

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange | ImmediateRef):
        self.target = target

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        for target in resolve_coil_targets_ctx(self.target, ctx):
            ctx.set_tag(target.name, True)


class ResetInstruction(Instruction):
    """Reset/Unlatch instruction (RST).

    Clears the target to its type's OFF/zero value, independent of its
    initialization default.
    """

    _reads = ()
    _writes = ("target",)
    _conditions = ()
    _structural_fields = ()

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange | ImmediateRef):
        self.target = target

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        for target in resolve_coil_targets_ctx(self.target, ctx):
            ctx.set_tag(target.name, reset_value_for_type(target.type))
