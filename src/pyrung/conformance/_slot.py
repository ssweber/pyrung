"""Slot template and fixed CLICK register layout for conformance cases."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pyrung.core import Int, named_array

if TYPE_CHECKING:
    from pyrung.core.structure import _NamedArrayRuntime

SLOT_FIELDS = ("Cmd", "Scratch", "Result1", "Result2", "Result3", "Result4", "Fired", "ErrorCode")

#: DS register that holds the program build hash (written on the first scan).
HASH_REGISTER = 1
#: DS register that holds the number of slots in the program (written on the first scan).
COUNT_REGISTER = 2
#: First DS register of slot 1.  DS1..DS10 are the harness header.
SLOT_BASE = 11
#: Registers per slot: 8 fields plus 2 spare, so slot N starts at DS(10*N + 1).
SLOT_STRIDE = 10
#: Highest DS register on a CLICK.
_DS_LAST = 4500
#: Most slots that fit in DS.
MAX_SLOTS = (_DS_LAST - SLOT_BASE + 1) // SLOT_STRIDE


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


_TEMPLATE: _NamedArrayRuntime = cast(Any, _SlotTemplate)


def make_slot(count: int) -> _NamedArrayRuntime:
    return _TEMPLATE.clone("Slot", count=count)


def slot_register(index: int, field: str = "Cmd") -> int:
    """Return the DS register number of *field* in slot *index* (1-based)."""
    return SLOT_BASE + (index - 1) * SLOT_STRIDE + SLOT_FIELDS.index(field)
