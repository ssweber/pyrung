"""Shared discovery of instruction write destinations.

Instruction classes declare ordinary destinations through ``_writes`` and
runtime status destinations through ``_status_fields``.  Analysis consumers
must read both declarations the same way: mappings contribute their values,
sequences contribute their elements, immediate references retain the wrapped
destination, and dynamic destinations remain opaque for their specialized
consumer to resolve.

The one derived shape is a statically sized sequential ``CopyInstruction``
fan-out.  Its additional destinations are concrete tags because the literal
source fixes the number of writes before execution.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.instruction.resolvers import _sequential_tags
from pyrung.core.memory_block import BlockRange
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
    yield value


def _static_copy_fanout(instr: Any) -> tuple[Tag, ...]:
    """Additional concrete destinations of a statically sized scalar copy."""
    if not isinstance(instr, CopyInstruction):
        return ()

    dest = instr.dest
    if isinstance(dest, ImmediateRef):
        dest = dest.value
    if not isinstance(dest, Tag):
        return ()

    source = instr.source
    converter = instr.convert
    count = 0
    if converter is None and isinstance(source, str) and len(source) > 1:
        if dest.type is TagType.CHAR:
            count = len(source)
    elif (
        converter is not None
        and getattr(converter, "mode", None) in {"value", "ascii"}
        and isinstance(source, str)
    ):
        count = len(source)

    if count <= 1:
        return ()
    try:
        return tuple(_sequential_tags(dest, count)[1:])
    except ValueError:
        return ()


def instruction_write_targets(instr: Any) -> tuple[Any, ...]:
    """Return every declared or statically derived write destination.

    Static ranges remain ranges so PDG extraction can retain range grouping.
    Indirect references and indirect ranges remain unresolved so PDG extraction
    can preserve address reads and conservative region attribution.
    """
    cls = type(instr)
    fields = tuple(
        dict.fromkeys((*getattr(cls, "_writes", ()), *getattr(cls, "_status_fields", ())))
    )
    targets: list[Any] = []
    for field_name in fields:
        targets.extend(_iter_target_leaves(getattr(instr, field_name, None)))
    targets.extend(_static_copy_fanout(instr))
    return tuple(targets)


def static_write_target_names(target: Any) -> frozenset[str]:
    """Concrete names represented by one safe, statically resolved target.

    Dynamic destinations deliberately return no names.  They need a runtime
    address or a PDG-specific conservative region, neither of which is an exact
    instruction occurrence for a named-tag writer lookup.
    """
    if isinstance(target, ImmediateRef):
        target = target.value
    if isinstance(target, Tag):
        return frozenset({target.name})
    if not isinstance(target, BlockRange):
        return frozenset()
    return frozenset(tag.name for tag in target.tags())


def instruction_writes_tag(instr: Any, tag_name: str) -> bool:
    """Whether *instr* has an exact static write destination named *tag_name*."""
    return any(
        tag_name in static_write_target_names(target) for target in instruction_write_targets(instr)
    )


__all__ = [
    "instruction_write_targets",
    "instruction_writes_tag",
    "static_write_target_names",
]
