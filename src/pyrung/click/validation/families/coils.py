"""The coil family: out(), latch() and reset().

laddercodec's Coil checks them.
"""

from __future__ import annotations

from typing import Any, assert_never

from laddercodec import Coil, CoilKind

from pyrung.core.instruction.operands import Coil as CoilTarget
from pyrung.core.instruction.operands import RangeArg, TagArg
from pyrung.core.validation.render import Call, declared_call

from ..fields import Family, Unbuildable, memory_types, refusals_of, render_operand
from ..resolve import _ResolutionCache

# The CLICK name of each coil that the family covers.
_KINDS: dict[str, CoilKind] = {
    "OutInstruction": "out",
    "LatchInstruction": "latch",
    "ResetInstruction": "reset",
}

# The argument of the pyrung call that holds each field of laddercodec's Coil.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Coil.operand": 0,
    "Coil.range_end": 0,
    "Coil.immediate": 0,
    "Coil.oneshot": "oneshot",
}

# The banks that hold bits. A coil in another bank has the register advice.
_BIT_BANKS = frozenset({"X", "Y", "C", "T", "CT", "SC"})


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    if code == "bank-not-allowed" and memory_types(instruction.target, resolver) - _BIT_BANKS:
        return "Write a register with copy() or fill()."
    return None


def immediate_call(instruction: Any) -> Call:
    """Return the declared call, with an ``immediate()`` target written as the author wrote it.

    The declared call prints the tag alone, so ``out(immediate(C1))`` would read ``out(C1)``.
    """
    call = declared_call(instruction)
    span = call.span(0)
    if not instruction.target.immediate or span is None:
        return call
    start, length = span
    text = f"{call.text[:start]}immediate({call.text[start : start + length]}){call.text[start + length :]}"
    shift = len("immediate()")
    arguments = tuple(
        (name, begin, size + shift, kind)
        if (begin, size) == (start, length)
        else (name, begin + shift if begin > start else begin, size, kind)
        for name, begin, size, kind in call.arguments
    )
    return Call(text, arguments, call.items)


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the coil that the export writes."""
    # The export refuses an immediate target outside Y itself. laddercodec gets the
    # address and the flag, and refuses it in its own words.
    coil: CoilTarget = instruction.target
    match coil:
        case TagArg(tag=target, immediate=immediate):
            pass
        case RangeArg(span=target, immediate=immediate):
            pass
        case _:
            assert_never(coil)
    text = render_operand(exporter, target, "target", instruction)
    # A list such as ``[C1,C5]`` is a range that CLICK cannot hold. Validation reports a
    # list for a plain target before this runs. The export refuses one in an immediate.
    if text.startswith("["):
        raise Unbuildable
    start, _sep, end = text.partition("..")
    kind = _KINDS[type(instruction).__name__]
    oneshot = bool(getattr(instruction, "oneshot", False))
    return refusals_of(
        lambda: Coil(start, kind=kind, range_end=end or None, immediate=immediate, oneshot=oneshot)
    )


FAMILY = Family(
    name="coils",
    classes={name: {"target": "target"} for name in _KINDS},
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    ranges={name: ("target",) for name in _KINDS},
    call_text=immediate_call,
    replaced_codes={
        "CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y": "immediate-coil-not-y",
        "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS": "CLK_COPY_RANGE_NOT_CONSECUTIVE",
    },
)
