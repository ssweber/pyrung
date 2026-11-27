"""Shared discovery of instruction write destinations.

Instruction classes declare ordinary destinations through ``_writes`` and
runtime status destinations through ``_status_fields``.  Analysis consumers
must read both declarations the same way: mappings contribute their values,
sequences contribute their elements, immediate references retain the wrapped
destination, and dynamic destinations remain opaque for their specialized
consumer to resolve.

The one derived shape is a ``CopyInstruction`` that writes text over several
elements.  A literal source fixes how many it writes.  A ``to_text()`` copy of a
tag writes as many characters as its value has, so the list holds every element
that the longest text of the source's type reaches.  The elements past the first
that such a copy may leave alone are its partial writes
(:func:`instruction_partial_write_targets`).
"""

from __future__ import annotations

import math
import sys
import weakref
from collections.abc import Iterator
from typing import Any, assert_never

from pyrung.core.instruction.conversions import (
    _DINT_MAX,
    _DINT_MIN,
    _INT_MAX,
    _INT_MIN,
    _render_text_from_numeric,
)
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.instruction.operands import (
    ConstArg,
    ExprArg,
    PointerArg,
    TagArg,
    Value,
    as_authored,
)
from pyrung.core.instruction.resolvers import _sequential_tags, _termination_char
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
    block_room,
)
from pyrung.core.storage import REAL_MAX
from pyrung.core.tag import ImmediateRef, Tag, TagType


def _iter_target_leaves(value: Any) -> Iterator[Any]:
    """Yield declared destination leaves without resolving dynamic addresses."""
    if value is None:
        return
    if isinstance(value, dict):
        for key in sorted(value, key=repr):
            yield from _iter_target_leaves(value[key])
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_target_leaves(item)
        return
    # The values of a function's outs are parsed operands.
    yield as_authored(value)


#: The values of each type whose text is the longest or the shortest that ``to_text()``
#: writes.  An integer's text grows with its magnitude and a minus sign.  A Real's fixed
#: text grows with its magnitude, and its exponential text with the digits of its
#: exponent.  pyrung keeps a Real at 64-bit precision, so the smallest one has a 3-digit
#: exponent.
_TEXT_EXTREMES: dict[TagType, tuple[Any, ...]] = {
    TagType.BOOL: (False, True),
    TagType.INT: (_INT_MIN, 0, _INT_MAX),
    TagType.DINT: (_DINT_MIN, 0, _DINT_MAX),
    TagType.WORD: (0, 0xFFFF),
    TagType.REAL: (
        *(
            sign * value
            for sign in (1.0, -1.0)
            for value in (REAL_MAX, sys.float_info.min * sys.float_info.epsilon, 0.0, math.inf)
        ),
        math.nan,
    ),
    TagType.CHAR: ("0", "9"),
}


def _source_text_type(source: Value) -> tuple[TagType, bool] | None:
    """Return the type a ``to_text()`` copy formats *source* as, and whether it reads the type.

    The copy formats a tag or a pointer by its type.  It formats an ``immediate()``
    tag by its value alone, as it does a literal.  Return ``None`` for an expression,
    whose text has no bound.
    """
    match source:
        case TagArg(tag=tag, immediate=immediate):
            return tag.type, not immediate
        case PointerArg(ref=ref):
            return ref.block.type, True
        case ExprArg() | ConstArg():
            return None
        case _:
            assert_never(source)


def _to_text_length(instr: CopyInstruction) -> int | None:
    """Return the most characters that a ``to_text()`` copy writes, with its termination code.

    The count comes from the formatter the copy runs, at the values of
    :data:`_TEXT_EXTREMES` for a tag.  A tag's ``min``/``max`` does not narrow it,
    because a CLICK does not honour them.  Return ``None`` for an expression source,
    whose text has no bound.
    """
    converter = instr.convert
    assert converter is not None and converter.mode == "text"
    tail = len(_termination_char(converter.termination_code))

    def length(value: Any, source_tag: Tag | None) -> int:
        try:
            text = _render_text_from_numeric(
                value,
                source_tag=source_tag,
                suppress_zero=converter.suppress_zero,
                pad=None,
                exponential=converter.exponential,
            )
        except (TypeError, ValueError, OverflowError):
            return 0  # the copy raises its fault and writes nothing
        return len(text) + tail

    source = instr.source
    if isinstance(source, ConstArg) and isinstance(source.value, (bool, int, float, str)):
        return length(source.value, None)
    typed = _source_text_type(source)
    if typed is None:
        return None
    source_type, by_type = typed
    source_tag = Tag("_text_source", source_type) if by_type else None
    return max(length(value, source_tag) for value in _TEXT_EXTREMES[source_type])


def _fanout_count(instr: CopyInstruction, dest_type: TagType) -> tuple[int | None, bool]:
    """Return how many elements *instr* writes from its destination on, and whether always.

    The count is ``None`` when it runs to the end of the block.
    """
    source = instr.source
    constant = source.value if isinstance(source, ConstArg) else None
    converter = instr.convert
    if converter is None:
        if isinstance(constant, str) and len(constant) > 1 and dest_type is TagType.CHAR:
            return len(constant), True
        return 1, True
    if converter.mode in {"value", "ascii"}:
        return (len(constant), True) if isinstance(constant, str) else (1, True)
    if converter.mode == "text" and dest_type is TagType.CHAR:
        literal = isinstance(constant, (bool, int, float, str))
        return _to_text_length(instr), literal
    return 1, True


def _static_fanout(dest: Tag, count: int | None) -> tuple[Tag, ...]:
    """Return the elements after *dest* that *count* characters from it reach in its block."""
    if count is None:
        count = block_room(dest)
        if count is None:
            # A tag in no block has no end to stop the text at.
            return ()
    if count <= 1:
        return ()
    try:
        return tuple(_sequential_tags(dest, count)[1:])
    except ValueError:
        return ()


def _pointer_fanout(dest: IndirectRef | IndirectExprRef, count: int | None) -> tuple[Any, ...]:
    """Return the elements after a pointer destination, as the addresses ``pointer + k``.

    Each consumer bounds ``block[pointer + k]`` from the pointer, as it bounds the
    destination itself.  Text of no bound covers the rest of the block.
    """
    expr = dest.pointer if isinstance(dest, IndirectRef) else dest.expr
    if count is None:
        return (IndirectBlockRange(dest.block, expr + 1, dest.block.end),)
    return tuple(IndirectExprRef(dest.block, expr + k) for k in range(1, count))


_Fanout = tuple[tuple[Any, ...], tuple[Any, ...]]
#: Per instruction, what its fan-out reads from the instruction alone: the count, and the
#: targets past a pointer destination.  A tag destination's targets are not kept.  They
#: depend on the bank the tag is mapped onto, which a later ``map_to`` moves, and holding
#: them would keep a bank alive that the mapping holds only weakly.
_FANOUT_CACHE: weakref.WeakKeyDictionary[Any, tuple[Any, ...]] = weakref.WeakKeyDictionary()


def _cached(instr: CopyInstruction) -> tuple[Any, ...] | None:
    cached = _FANOUT_CACHE.get(instr)
    if (
        cached is not None
        and cached[0] is instr.dest
        and cached[1] is instr.source
        and cached[2] is instr.convert
    ):
        return cached
    return None


def _copy_fanout(instr: Any) -> _Fanout:
    """Return the destinations past the first of a copy that writes several elements.

    The result holds the ones it always writes, then the ones it may leave alone.  A
    pointer destination's targets are the same objects on every call, so a consumer
    that keys them by ``id`` finds them again.
    """
    if not isinstance(instr, CopyInstruction):
        return (), ()
    match instr.dest:
        case TagArg(tag=dest):
            cached = _cached(instr)
            if cached is None:
                cached = (instr.dest, instr.source, instr.convert, _fanout_count(instr, dest.type))
                _FANOUT_CACHE[instr] = cached
            count, always = cached[3]
            extra = _static_fanout(dest, count)
            return (extra, ()) if always else ((), extra)
        case PointerArg(ref=dest):
            cached = _cached(instr)
            if cached is None:
                # A copy through a pointer may write any element that the pointer reaches.
                # Each of those elements gets a possible write.
                count, _always = _fanout_count(instr, dest.block.type)
                partial = _pointer_fanout(dest, count) if count is None or count > 1 else ()
                cached = (instr.dest, instr.source, instr.convert, partial)
                _FANOUT_CACHE[instr] = cached
            return (), cached[3]
        case _:
            assert_never(instr.dest)


def instruction_write_targets(instr: Any, *, status: bool = True) -> tuple[Any, ...]:
    """Return every declared or statically derived write destination.

    Static ranges remain ranges so PDG extraction can retain range grouping.
    Indirect references and indirect ranges remain unresolved so PDG extraction
    can preserve address reads and conservative region attribution.  With
    *status* false, the instruction's own status fields (a timer's EN and TT)
    are left out.
    """
    cls = type(instr)
    status_fields = getattr(cls, "_status_fields", ()) if status else ()
    fields = tuple(dict.fromkeys((*getattr(cls, "_writes", ()), *status_fields)))
    targets: list[Any] = []
    for field_name in fields:
        targets.extend(_iter_target_leaves(as_authored(getattr(instr, field_name, None))))
    definite, partial = _copy_fanout(instr)
    targets.extend(definite)
    targets.extend(partial)
    return tuple(targets)


def instruction_partial_write_targets(instr: Any) -> tuple[Any, ...]:
    """Return the targets of :func:`instruction_write_targets` that *instr* may leave alone.

    A ``to_text()`` copy of a tag writes only as many characters as its value has.  A
    write-before-read analysis must not count these as writes that always happen.
    """
    return _copy_fanout(instr)[1]


def instruction_fault_writes(instr: Any) -> tuple[str, ...]:
    """Return the system fault flags that *instr* can set, from its ``_fault_writes`` declaration.

    This is separate from :func:`instruction_write_targets`.  Fault flags are
    read-only system tags that the program cannot address, so write-target
    consumers (read-only and ownership lints) must not see them.
    """
    return tuple(getattr(instr, "_fault_writes", ()))


def static_write_target_tags(target: Any) -> tuple[Tag, ...]:
    """Return the tags that one statically resolved target names, in address order.

    Dynamic destinations deliberately return no tags.  They need a runtime
    address or a PDG-specific conservative region, and neither is an exact
    instruction occurrence for a named-tag writer lookup.
    """
    target = as_authored(target)
    if isinstance(target, ImmediateRef):
        target = target.value
    if isinstance(target, Tag):
        return (target,)
    if not isinstance(target, BlockRange):
        return ()
    return tuple(target.tags())


def static_write_target_names(target: Any) -> frozenset[str]:
    """Return the concrete names that one safe, statically resolved target represents."""
    return frozenset(tag.name for tag in static_write_target_tags(target))


def instruction_written_tags(instr: Any, *, status: bool = True) -> tuple[Tag, ...]:
    """Return each tag that *instr* may write at a static address, once, in declaration order."""
    tags: dict[str, Tag] = {}
    for target in instruction_write_targets(instr, status=status):
        for tag in static_write_target_tags(target):
            tags.setdefault(tag.name, tag)
    return tuple(tags.values())


def instruction_writes_tag(instr: Any, tag_name: str) -> bool:
    """Whether *instr* has an exact static write destination named *tag_name*."""
    return any(
        tag_name in static_write_target_names(target) for target in instruction_write_targets(instr)
    )


__all__ = [
    "instruction_fault_writes",
    "instruction_partial_write_targets",
    "instruction_write_targets",
    "instruction_writes_tag",
    "instruction_written_tags",
    "static_write_target_names",
    "static_write_target_tags",
]
