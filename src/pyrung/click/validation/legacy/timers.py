"""pyrung's checks of on_delay() and off_delay()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..checks import Check

if TYPE_CHECKING:
    pass


CODES: tuple[str, ...] = ()

CHECKS: tuple[Check, ...] = ()

_TIMER_ROLES = (
    ("done_bit", "timer_done_bit"),
    ("accumulator", "timer_accumulator"),
    ("preset", "timer_preset"),
)

TABLES: dict[str, dict[Any, Any]] = {
    "ROLE_ASSIGNMENT_FIELDS": {
        "OnDelayInstruction": _TIMER_ROLES,
        "OffDelayInstruction": _TIMER_ROLES,
    },
    "TIMER_PAIR_FIELDS": {
        "OnDelayInstruction": (("done_bit", "accumulator"),),
        "OffDelayInstruction": (("done_bit", "accumulator"),),
    },
    "TIMER_PRESET_FIELDS": {
        "OnDelayInstruction": ("preset",),
        "OffDelayInstruction": ("preset",),
    },
}
