"""pyrung's checks of out(), latch() and reset()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.validation.walker import ProgramLocation

from ..checks import Check
from ..findings import (
    CLK_BANK_WRONG_ROLE,
    ClickFinding,
    ValidationMode,
    _route_severity,
)
from ..resolve import (
    _bank_label,
    _format_location,
    _instruction_location,
    _ResolutionCache,
)

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile


# Maps each coil instruction to its pyrung verb and CLICK coil name. The target must be
# bit memory. toggle() and the Out variants already accept only a Bool tag.
_COIL_TARGET_INSTRUCTIONS: dict[str, tuple[str, str]] = {
    "OutInstruction": ("out", "Out"),
    "LatchInstruction": ("latch", "Set"),
    "ResetInstruction": ("reset", "Reset"),
}


def _evaluate_coil_targets(
    instruction: Any,
    base_location: ProgramLocation,
    profile: HardwareProfile,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    names = _COIL_TARGET_INSTRUCTIONS.get(type(instruction).__name__)
    if names is None:
        return []
    verb, coil = names
    # An unresolved target is reported by the write-target check.
    resolution = resolver.resolve_operand_slots(instruction.target)
    bad = [
        _bank_label(slot)
        for slot in resolution.slots
        if not profile.valid_for_role(slot.memory_type, "coil_target")
    ]
    if not bad:
        return []
    target = bad[0] if len(bad) == 1 else f"{bad[0]}..{bad[-1]}"
    location_text = _format_location(_instruction_location(base_location, "instruction.target"))
    return [
        ClickFinding(
            code=CLK_BANK_WRONG_ROLE,
            severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
            message=(
                f"{verb}({target}) at {location_text}: CLICK's {coil} coil takes bit "
                "memory only (X, Y, C, T, CT, SC). Write a register with copy() or fill()."
            ),
            location=location_text,
        )
    ]


CODES: tuple[str, ...] = ()

CHECKS = (
    Check(
        "profile",
        30,
        lambda instruction, location, context: _evaluate_coil_targets(
            instruction, location, context.profile, context.mode, context.resolver
        ),
    ),
)

TABLES: dict[str, dict[Any, Any]] = {
    "WRITE_TARGET_FIELDS": {
        "OutInstruction": ("target",),
        "LatchInstruction": ("target",),
        "ResetInstruction": ("target",),
    },
    "IMMEDIATE_COIL_TARGETS": {
        "OutInstruction": "target",
        "LatchInstruction": "target",
        "ResetInstruction": "target",
    },
    "COIL_TARGET_INSTRUCTIONS": _COIL_TARGET_INSTRUCTIONS,
}
