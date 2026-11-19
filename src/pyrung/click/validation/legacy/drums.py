"""pyrung's checks of event_drum(), time_drum() and time_event_drum()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.condition import Condition
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation.walker import ProgramLocation, _condition_children

from ..checks import Check
from ..findings import (
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_WRONG_ROLE,
    ClickFinding,
    ValidationMode,
    _route_severity,
)
from ..hardware import _not_writable_text, _role_banks, _role_words
from ..resolve import (
    _format_location,
    _instruction_location,
    _ResolutionCache,
    _unresolved_finding,
    _wrong_role_text,
)

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile
    from pyrung.click.tag_map import TagMap


CLK_DRUM_JUMP_STEP_TAG_REQUIRED = "CLK_DRUM_JUMP_STEP_TAG_REQUIRED"
CODES = (CLK_DRUM_JUMP_STEP_TAG_REQUIRED,)


def _iter_condition_tags(root: Any) -> tuple[Tag, ...]:
    found: list[Tag] = []
    seen_values: set[int] = set()
    seen_tags: set[int] = set()

    def walk(value: Any) -> None:
        value_id = id(value)
        if value_id in seen_values:
            return
        seen_values.add(value_id)

        if isinstance(value, Tag):
            tag_id = id(value)
            if tag_id not in seen_tags:
                seen_tags.add(tag_id)
                found.append(value)
            return

        if isinstance(value, ImmediateRef):
            walk(value.value)
            return

        if isinstance(value, Condition):
            for _, child in _condition_children(value):
                walk(child)
            return

    walk(root)
    return tuple(found)


def _evaluate_drums(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    instruction_type = type(instruction).__name__
    if instruction_type not in {
        "EventDrumInstruction",
        "TimeDrumInstruction",
        "TimeEventDrumInstruction",
    }:
        return []

    findings: list[ClickFinding] = []

    def check_role(
        value: Any,
        role: str,
        location: ProgramLocation,
        unresolved_reason: str,
    ) -> None:
        resolution = resolver.resolve_operand_slots(value)
        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, unresolved_reason))
            return
        for slot in resolution.slots:
            if not profile.valid_for_role(slot.memory_type, role):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_WRONG_ROLE,
                        severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
                        message=_wrong_role_text(
                            slot, _role_words(role), location_text, _role_banks(profile, role)
                        ),
                        location=location_text,
                    )
                )

    outputs = getattr(instruction, "outputs", ())
    for idx, output in enumerate(outputs):
        location = _instruction_location(base_location, f"instruction.outputs[{idx}]")
        resolution = resolver.resolve_operand_slots(output)
        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "drum output"))
            continue
        for slot in resolution.slots:
            if not profile.is_writable(slot.memory_type, slot.address):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_NOT_WRITABLE,
                        severity=_route_severity(CLK_BANK_NOT_WRITABLE, mode),
                        message=_not_writable_text(instruction, slot, location_text),
                        location=location_text,
                    )
                )
            if not profile.valid_for_role(slot.memory_type, "drum_output_bit"):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_WRONG_ROLE,
                        severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
                        message=_wrong_role_text(
                            slot,
                            "drum output",
                            location_text,
                            _role_banks(profile, "drum_output_bit"),
                        ),
                        location=location_text,
                    )
                )

    if instruction_type in {"EventDrumInstruction", "TimeEventDrumInstruction"}:
        events = getattr(instruction, "events", ())
        for idx, event_condition in enumerate(events):
            if event_condition is None:
                continue
            event_location = _instruction_location(base_location, f"instruction.events[{idx}]")
            event_tags = _iter_condition_tags(event_condition)
            for event_tag in event_tags:
                check_role(
                    value=event_tag,
                    role="drum_event_condition",
                    location=event_location,
                    unresolved_reason="drum event condition",
                )

    jump_step = getattr(instruction, "jump_step", None)
    if jump_step is not None and not isinstance(jump_step, Tag):
        location = _instruction_location(base_location, "instruction.jump_step")
        location_text = _format_location(location)
        findings.append(
            ClickFinding(
                code=CLK_DRUM_JUMP_STEP_TAG_REQUIRED,
                severity=_route_severity(CLK_DRUM_JUMP_STEP_TAG_REQUIRED, mode),
                message=(
                    f"The drum's jump step at {location_text} is not a tag. CLICK needs a DS "
                    "tag there."
                ),
                location=location_text,
            )
        )

    return findings


CHECKS = (
    Check(
        "profile",
        40,
        lambda instruction, location, context: _evaluate_drums(
            instruction, location, context.tag_map, context.profile, context.mode, context.resolver
        ),
    ),
)

TABLES: dict[str, dict[Any, Any]] = {
    "WRITE_TARGET_FIELDS": {
        "EventDrumInstruction": ("current_step", "completion_flag"),
        "TimeDrumInstruction": ("current_step", "accumulator", "completion_flag"),
        "TimeEventDrumInstruction": ("current_step", "accumulator", "completion_flag"),
    },
    "ROLE_ASSIGNMENT_FIELDS": {
        "EventDrumInstruction": (
            ("current_step", "drum_current_step"),
            ("completion_flag", "drum_completion_flag"),
            ("jump_step", "drum_jump_step"),
        ),
        "TimeDrumInstruction": (
            ("current_step", "drum_current_step"),
            ("accumulator", "drum_accumulator"),
            ("completion_flag", "drum_completion_flag"),
            ("jump_step", "drum_jump_step"),
        ),
        "TimeEventDrumInstruction": (
            ("current_step", "drum_current_step"),
            ("accumulator", "drum_accumulator"),
            ("completion_flag", "drum_completion_flag"),
            ("jump_step", "drum_jump_step"),
        ),
    },
    # A drum step accepts a constant preset of 0 and refuses a negative one (checked in
    # CLICK v4's editor, 2026-11-06).
    "CONSTANT_PRESET_FLOORS": {
        "TimeDrumInstruction": ("time_drum", 0, ()),
        "TimeEventDrumInstruction": ("time_event_drum", 0, ()),
    },
}
