"""Slot template for twin harness tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pyrung.core import Int, named_array

if TYPE_CHECKING:
    from pyrung.core.structure import _NamedArrayRuntime

SLOT_FIELDS = ("Cmd", "Scratch", "Result1", "Result2", "Result3", "Result4", "Fired", "ErrorCode")


@named_array(Int, count=1, stride=8)
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
