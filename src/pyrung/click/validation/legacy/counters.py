"""pyrung's checks of count_up() and count_down()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..checks import Check

if TYPE_CHECKING:
    pass


CODES: tuple[str, ...] = ()

CHECKS: tuple[Check, ...] = ()

_COUNTER_ROLES = (
    ("done_bit", "counter_done_bit"),
    ("accumulator", "counter_accumulator"),
    ("preset", "counter_preset"),
)

TABLES: dict[str, dict[Any, Any]] = {
    "ROLE_ASSIGNMENT_FIELDS": {
        "CountUpInstruction": _COUNTER_ROLES,
        "CountDownInstruction": _COUNTER_ROLES,
    },
    "TIMER_PAIR_FIELDS": {
        "CountUpInstruction": (("done_bit", "accumulator"),),
        "CountDownInstruction": (("done_bit", "accumulator"),),
    },
}
