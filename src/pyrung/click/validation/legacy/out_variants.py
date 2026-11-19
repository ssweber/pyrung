"""pyrung's checks of toggle(), debounce(), flasher() and timed()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.time_mode import TimeUnit
from pyrung.core.validation.walker import ProgramLocation

from ..checks import Check
from ..findings import ClickFinding, ValidationMode, _route_severity
from ..resolve import (
    _format_location,
    _instruction_location,
)

if TYPE_CHECKING:
    pass


CLK_OUT_UNIT_UNSUPPORTED = "CLK_OUT_UNIT_UNSUPPORTED"
CODES = (CLK_OUT_UNIT_UNSUPPORTED,)


# CLICK v4's Out dialog offers fewer time units than its timers do.
_OUT_VARIANT_UNITS: dict[str, tuple[str, frozenset[TimeUnit]]] = {
    "DebounceInstruction": ("debounce", frozenset({TimeUnit.Tms, TimeUnit.Ts})),
    "FlasherInstruction": ("flasher", frozenset({TimeUnit.Tms, TimeUnit.Ts})),
    "TimedInstruction": ("timed", frozenset({TimeUnit.Tms, TimeUnit.Ts, TimeUnit.Tm})),
}


_UNIT_NAMES = {
    TimeUnit.Tms: "ms",
    TimeUnit.Ts: "sec",
    TimeUnit.Tm: "min",
    TimeUnit.Th: "hour",
    TimeUnit.Td: "day",
}


def _evaluate_out_variant_unit(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
) -> list[ClickFinding]:
    spec = _OUT_VARIANT_UNITS.get(type(instruction).__name__)
    if spec is None:
        return []
    verb, allowed = spec
    unit = instruction.unit
    if unit in allowed:
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.unit"))
    choices = "/".join(_UNIT_NAMES[u] for u in TimeUnit if u in allowed)
    return [
        ClickFinding(
            code=CLK_OUT_UNIT_UNSUPPORTED,
            severity=_route_severity(CLK_OUT_UNIT_UNSUPPORTED, mode),
            message=(
                f"CLICK's {verb} coil times in {choices}, not {_UNIT_NAMES[unit]}, "
                f"at {location_text}."
            ),
            location=location_text,
            suggestion=f"Use unit= one of {choices} and scale the preset.",
        )
    ]


CHECKS = (
    Check(
        "instruction",
        60,
        lambda instruction, location, context: _evaluate_out_variant_unit(
            instruction, location, context.mode
        ),
    ),
)

TABLES: dict[str, dict[Any, Any]] = {
    "WRITE_TARGET_FIELDS": {
        "ToggleInstruction": ("target",),
        "DebounceInstruction": ("target",),
        "FlasherInstruction": ("target",),
        "TimedInstruction": ("target",),
    },
    "IMMEDIATE_COIL_TARGETS": {"ToggleInstruction": "target"},
    # Each Out variant claims each phase's whole Timer, as on_delay() does.
    "ROLE_ASSIGNMENT_FIELDS": {
        "DebounceInstruction": (
            ("on_done", "timer_done_bit"),
            ("on_acc", "timer_accumulator"),
            ("on_preset", "timer_preset"),
            ("off_done", "timer_done_bit"),
            ("off_acc", "timer_accumulator"),
            ("off_preset", "timer_preset"),
        ),
        "FlasherInstruction": (
            ("on_done", "timer_done_bit"),
            ("on_acc", "timer_accumulator"),
            ("on_preset", "timer_preset"),
            ("off_done", "timer_done_bit"),
            ("off_acc", "timer_accumulator"),
            ("off_preset", "timer_preset"),
        ),
        "TimedInstruction": (
            ("timer_done", "timer_done_bit"),
            ("timer_acc", "timer_accumulator"),
            ("on_preset", "timer_preset"),
        ),
    },
    "TIMER_PAIR_FIELDS": {
        "DebounceInstruction": (("on_done", "on_acc"), ("off_done", "off_acc")),
        "FlasherInstruction": (("on_done", "on_acc"), ("off_done", "off_acc")),
        "TimedInstruction": (("timer_done", "timer_acc"),),
    },
    "TIMER_PRESET_FIELDS": {
        "DebounceInstruction": ("on_preset", "off_preset"),
        "FlasherInstruction": ("on_preset", "off_preset"),
        "TimedInstruction": ("on_preset",),
    },
    # The least constant preset that CLICK v4's dialogs accept (checked in the editor,
    # 2026-11-06). A flasher or a timed coil refuses 0. A debounce accepts 0 and refuses a
    # negative one. A register preset accepts any value.
    "CONSTANT_PRESET_FLOORS": {
        "DebounceInstruction": ("debounce", 0, ("on_preset", "off_preset")),
        "FlasherInstruction": ("flasher", 1, ("on_preset", "off_preset")),
        "TimedInstruction": ("timed", 1, ("on_preset",)),
    },
    "OUT_VARIANT_UNITS": _OUT_VARIANT_UNITS,
}
