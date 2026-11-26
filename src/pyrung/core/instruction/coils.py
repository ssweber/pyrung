"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, assert_never

from pyrung.core.storage import BLANK_CHAR
from pyrung.core.tag import Tag, TagType

from .base import Instruction, OneShotMixin
from .operands import COIL, Coil, RangeArg, TagArg

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


def coil_tags(target: Coil) -> tuple[Tag, ...]:
    """Return the tags that a coil with *target* writes, in the order that it writes them."""
    match target:
        case TagArg(tag=tag):
            return (tag,)
        case RangeArg(span=span):
            return tuple(span.tags())
        case _:
            assert_never(target)


_RESET_VALUES: dict[TagType, bool | int | float | str] = {
    TagType.BOOL: False,
    TagType.INT: 0,
    TagType.DINT: 0,
    TagType.REAL: 0.0,
    TagType.WORD: 0,
    TagType.CHAR: BLANK_CHAR,
}


def reset_value_for_type(tag_type: TagType) -> bool | int | float | str:
    """Return the OFF/zero value written by a RESET instruction."""
    return _RESET_VALUES[tag_type]


class OutInstruction(OneShotMixin, Instruction):
    """Output coil instruction (OUT).

    Sets the target bit to True when executed.

    ``__init__`` parses the target into a ``Coil`` (:mod:`pyrung.core.instruction.operands`).
    """

    INERT_WHEN_DISABLED = False
    _reads = ()
    _writes = ("target",)
    _conditions = ()
    _structural_fields = ()
    _call_operands = (("target", "target"),)

    def __init__(self, target: Any, *, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.target: Coil = COIL.parse("out()", "target", target)
        self._static_targets = coil_tags(self.target)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        targets = self._static_targets
        if not enabled:
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
        for target in targets:
            ctx.set_tag(target.name, True)


class LatchInstruction(Instruction):
    """Latch/Set instruction (SET).

    Sets the target bit to True. Unlike OUT, this is typically
    not reset when the rung goes false.

    ``__init__`` parses the target into a ``Coil`` (:mod:`pyrung.core.instruction.operands`).
    """

    _reads = ()
    _writes = ("target",)
    _conditions = ()
    _structural_fields = ()
    _call_operands = (("target", "target"),)
    _call_keywords = ()

    def __init__(self, target: Any):
        self.target: Coil = COIL.parse("latch()", "target", target)
        self._static_targets = coil_tags(self.target)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        for target in self._static_targets:
            ctx.set_tag(target.name, True)


class ResetInstruction(Instruction):
    """Reset/Unlatch instruction (RST).

    Clears the target to its type's OFF/zero value, independent of its
    initialization default.

    ``__init__`` parses the target into a ``Coil`` (:mod:`pyrung.core.instruction.operands`).
    """

    _reads = ()
    _writes = ("target",)
    _conditions = ()
    _structural_fields = ()
    _call_operands = (("target", "target"),)
    _call_keywords = ()

    def __init__(self, target: Any):
        self.target: Coil = COIL.parse("reset()", "target", target)
        self._static_targets = coil_tags(self.target)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            return
        for target in self._static_targets:
            ctx.set_tag(target.name, reset_value_for_type(target.type))
