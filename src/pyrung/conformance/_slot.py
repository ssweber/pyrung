"""Slot template and fixed CLICK register layout for conformance cases.

DS1..DS10 are the program header.  Each group owns a block of DS registers
starting at its ``base``: the first 10 registers are the group header, then
slot N of the group lives at ``base + 10*N`` .. ``base + 10*N + 7``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pyrung.core import Int, named_array

if TYPE_CHECKING:
    from pyrung.core.structure import _NamedArrayRuntime

SLOT_FIELDS = (
    "Cmd",
    "Scratch",
    "Result1",
    "Result2",
    "Result3",
    "Result4",
    "Fired",
    "ErrorCode",
    "Ready",
)

#: DS register that holds the program (layout) hash, written every scan by the main program.
PROGRAM_HASH_REGISTER = 1
#: DS1..DS10 are the program header; group blocks start after it.
HEADER_END = 10
#: Registers per slot (9 fields plus 1 spare); also the size of a group header.
SLOT_STRIDE = 10
#: Highest DS register on a CLICK.
DS_LAST = 4500


@named_array(Int, count=1, stride=SLOT_STRIDE)
class _SlotTemplate:
    Cmd = 0
    Scratch = 0
    Result1 = 0
    Result2 = 0
    Result3 = 0
    Result4 = 0
    Fired = 0
    ErrorCode = 0
    Ready = 0


_TEMPLATE: _NamedArrayRuntime = cast(Any, _SlotTemplate)


def make_slot(count: int, name: str = "Slot") -> _NamedArrayRuntime:
    return _TEMPLATE.clone(name, count=count)


def block_size(slots: int) -> int:
    """DS registers a group with *slots* slot capacity occupies, header included."""
    return (slots + 1) * SLOT_STRIDE


def slot_register(base: int, index: int, field: str = "Cmd") -> int:
    """Return the DS register of *field* in slot *index* (1-based) of the group at *base*."""
    return base + index * SLOT_STRIDE + SLOT_FIELDS.index(field)
