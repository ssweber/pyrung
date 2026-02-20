"""Automatically generated module split."""

from __future__ import annotations

from pyrung.core.tag import Tag

from .base import Instruction, OneShotMixin
from .resolvers import (
    resolve_coil_targets_ctx,
)


class OutInstruction(OneShotMixin, Instruction):
    """Output coil instruction (OUT).

    Sets the target bit to True when executed.
    """

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.target = target

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        targets = resolve_coil_targets_ctx(self.target, ctx)
        if not enabled:
            self.reset_oneshot()
            for target in targets:
                ctx.set_tag(target.name, False)
            return

        if not self.should_execute(enabled):
            return
        for target in targets:
            ctx.set_tag(target.name, True)

    def is_inert_when_disabled(self) -> bool:
        return False


class LatchInstruction(Instruction):
    """Latch/Set instruction (SET).

    Sets the target bit to True. Unlike OUT, this is typically
    not reset when the rung goes false.
    """

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange):
        self.target = target

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        for target in resolve_coil_targets_ctx(self.target, ctx):
            ctx.set_tag(target.name, True)


class ResetInstruction(Instruction):
    """Reset/Unlatch instruction (RST).

    Sets the target to its default value (False for bits, 0 for ints).
    """

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange):
        self.target = target

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        for target in resolve_coil_targets_ctx(self.target, ctx):
            ctx.set_tag(target.name, target.default)
