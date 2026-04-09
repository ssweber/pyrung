"""Hardware profile checks for Click validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.click.capabilities import CONVERTER_COMPATIBILITY
from pyrung.core.condition import Condition
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation.walker import ProgramLocation, _condition_children

from .findings import (
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_WRONG_ROLE,
    CLK_COPY_BANK_INCOMPATIBLE,
    CLK_COPY_CONVERTER_INCOMPATIBLE,
    CLK_DRUM_JUMP_STEP_TAG_REQUIRED,
    CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED,
    CLK_PACK_TEXT_BANK_INCOMPATIBLE,
    CLK_TIMER_PRESET_OVERFLOW,
    ClickFinding,
    ValidationMode,
    _build_suggestion,
    _route_severity,
)
from .resolve import (
    _bank_label,
    _format_location,
    _instruction_location,
    _resolve_operand_slots,
    _unresolved_finding,
)

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile
    from pyrung.click.tag_map import TagMap

# ---------------------------------------------------------------------------
# Rule maps (table-driven)
# ---------------------------------------------------------------------------

_WRITE_TARGET_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("OutInstruction", "target"),
        ("LatchInstruction", "target"),
        ("ResetInstruction", "target"),
        ("CopyInstruction", "target"),
        ("BlockCopyInstruction", "dest"),
        ("FillInstruction", "dest"),
        ("CalcInstruction", "dest"),
        ("SearchInstruction", "result"),
        ("SearchInstruction", "found"),
        ("ShiftInstruction", "bit_range"),
        ("PackBitsInstruction", "dest"),
        ("PackWordsInstruction", "dest"),
        ("UnpackToBitsInstruction", "bit_block"),
        ("UnpackToWordsInstruction", "word_block"),
        ("PackTextInstruction", "dest"),
        ("EventDrumInstruction", "current_step"),
        ("EventDrumInstruction", "completion_flag"),
        ("TimeDrumInstruction", "current_step"),
        ("TimeDrumInstruction", "accumulator"),
        ("TimeDrumInstruction", "completion_flag"),
    }
)

_ROLE_ASSIGNMENT_FIELDS: dict[tuple[str, str], str] = {
    ("OnDelayInstruction", "done_bit"): "timer_done_bit",
    ("OnDelayInstruction", "accumulator"): "timer_accumulator",
    ("OnDelayInstruction", "preset"): "timer_preset",
    ("OffDelayInstruction", "done_bit"): "timer_done_bit",
    ("OffDelayInstruction", "accumulator"): "timer_accumulator",
    ("OffDelayInstruction", "preset"): "timer_preset",
    ("CountUpInstruction", "done_bit"): "counter_done_bit",
    ("CountUpInstruction", "accumulator"): "counter_accumulator",
    ("CountUpInstruction", "preset"): "counter_preset",
    ("CountDownInstruction", "done_bit"): "counter_done_bit",
    ("CountDownInstruction", "accumulator"): "counter_accumulator",
    ("CountDownInstruction", "preset"): "counter_preset",
    ("EventDrumInstruction", "current_step"): "drum_current_step",
    ("EventDrumInstruction", "completion_flag"): "drum_completion_flag",
    ("EventDrumInstruction", "jump_step"): "drum_jump_step",
    ("TimeDrumInstruction", "current_step"): "drum_current_step",
    ("TimeDrumInstruction", "accumulator"): "drum_accumulator",
    ("TimeDrumInstruction", "completion_flag"): "drum_completion_flag",
    ("TimeDrumInstruction", "jump_step"): "drum_jump_step",
}

_COPY_COMPATIBILITY_FIELDS: dict[str, tuple[str, str, str]] = {
    "CopyInstruction": ("single", "source", "target"),
    "BlockCopyInstruction": ("block", "source", "dest"),
    "FillInstruction": ("fill", "value", "dest"),
    "PackBitsInstruction": ("pack_bits", "bit_block", "dest"),
    "PackWordsInstruction": ("pack_words", "word_block", "dest"),
    "UnpackToBitsInstruction": ("unpack_bits", "source", "bit_block"),
    "UnpackToWordsInstruction": ("unpack_words", "source", "word_block"),
}


def _load_default_profile() -> HardwareProfile | None:
    from pyrung.click.profile import load_default_profile

    return load_default_profile()


def _evaluate_write_targets(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for candidate_type, field_name in _WRITE_TARGET_FIELDS:
        if instruction_type != candidate_type:
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = _resolve_operand_slots(getattr(instruction, field_name), tag_map)

        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "mapping missing or ambiguous"))
            continue

        for slot in resolution.slots:
            if slot.address is None and slot.memory_type in {"SC", "SD"}:
                findings.append(
                    _unresolved_finding(location, mode, "address required for SC/SD writability")
                )
                continue

            if not profile.is_writable(slot.memory_type, slot.address):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_NOT_WRITABLE,
                        severity=_route_severity(CLK_BANK_NOT_WRITABLE, mode),
                        message=(
                            f"Write target {_bank_label(slot)} is not writable for Click "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    return findings


def _evaluate_role_assignments(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for (candidate_type, field_name), role in _ROLE_ASSIGNMENT_FIELDS.items():
        if instruction_type != candidate_type:
            continue

        value = getattr(instruction, field_name)
        if field_name in {"preset", "jump_step"} and not isinstance(value, Tag):
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = _resolve_operand_slots(value, tag_map)

        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "mapping missing or ambiguous"))
            continue

        for slot in resolution.slots:
            if not profile.valid_for_role(slot.memory_type, role):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_WRONG_ROLE,
                        severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
                        message=(
                            f"Bank {_bank_label(slot)} is invalid for role {role} "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    return findings


def _evaluate_copy_compatibility(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__
    mapping = _COPY_COMPATIBILITY_FIELDS.get(instruction_type)
    if mapping is None:
        return findings

    operation, source_field, dest_field = mapping
    source_location = _instruction_location(base_location, f"instruction.{source_field}")
    dest_location = _instruction_location(base_location, f"instruction.{dest_field}")

    source_resolution = _resolve_operand_slots(getattr(instruction, source_field), tag_map)
    dest_resolution = _resolve_operand_slots(getattr(instruction, dest_field), tag_map)

    if source_resolution.unresolved:
        findings.append(_unresolved_finding(source_location, mode, "source bank unresolved"))
    if dest_resolution.unresolved:
        findings.append(_unresolved_finding(dest_location, mode, "destination bank unresolved"))
    if source_resolution.unresolved or dest_resolution.unresolved:
        return findings

    if not source_resolution.slots or not dest_resolution.slots:
        return findings

    for source_slot in source_resolution.slots:
        for dest_slot in dest_resolution.slots:
            if not profile.copy_compatible(
                operation, source_slot.memory_type, dest_slot.memory_type
            ):
                location_text = _format_location(dest_location)
                findings.append(
                    ClickFinding(
                        code=CLK_COPY_BANK_INCOMPATIBLE,
                        severity=_route_severity(CLK_COPY_BANK_INCOMPATIBLE, mode),
                        message=(
                            f"Copy operation {operation} is incompatible for "
                            f"{source_slot.memory_type} -> {dest_slot.memory_type} "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    # --- Converter / bank compatibility ---
    converter = getattr(instruction, "convert", None)
    if isinstance(converter, CopyConverter):
        compat = CONVERTER_COMPATIBILITY.get(converter.mode)
        if compat is not None:
            valid_sources, valid_dests = compat
            for source_slot in source_resolution.slots:
                if source_slot.memory_type not in valid_sources:
                    location_text = _format_location(source_location)
                    findings.append(
                        ClickFinding(
                            code=CLK_COPY_CONVERTER_INCOMPATIBLE,
                            severity=_route_severity(CLK_COPY_CONVERTER_INCOMPATIBLE, mode),
                            message=(
                                f"Converter to_{converter.mode} requires source bank in "
                                f"{sorted(valid_sources)}, got {source_slot.memory_type} "
                                f"at {location_text}."
                            ),
                            location=location_text,
                            suggestion=_build_suggestion(
                                CLK_COPY_CONVERTER_INCOMPATIBLE, None, tag_map
                            ),
                        )
                    )
            for dest_slot in dest_resolution.slots:
                if dest_slot.memory_type not in valid_dests:
                    location_text = _format_location(dest_location)
                    findings.append(
                        ClickFinding(
                            code=CLK_COPY_CONVERTER_INCOMPATIBLE,
                            severity=_route_severity(CLK_COPY_CONVERTER_INCOMPATIBLE, mode),
                            message=(
                                f"Converter to_{converter.mode} requires destination bank in "
                                f"{sorted(valid_dests)}, got {dest_slot.memory_type} "
                                f"at {location_text}."
                            ),
                            location=location_text,
                            suggestion=_build_suggestion(
                                CLK_COPY_CONVERTER_INCOMPATIBLE, None, tag_map
                            ),
                        )
                    )

    return findings


def _evaluate_pack_text(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    if type(instruction).__name__ != "PackTextInstruction":
        return []

    findings: list[ClickFinding] = []
    source_location = _instruction_location(base_location, "instruction.source_range")
    dest_location = _instruction_location(base_location, "instruction.dest")

    source_resolution = _resolve_operand_slots(instruction.source_range, tag_map)
    dest_resolution = _resolve_operand_slots(instruction.dest, tag_map)

    if source_resolution.unresolved:
        findings.append(_unresolved_finding(source_location, mode, "source bank unresolved"))
    if dest_resolution.unresolved:
        findings.append(_unresolved_finding(dest_location, mode, "destination bank unresolved"))
    if source_resolution.unresolved or dest_resolution.unresolved:
        return findings

    if not source_resolution.slots or not dest_resolution.slots:
        return findings

    allowed_dest_banks = {"DS", "DD", "DH", "DF", "TD", "CTD"}
    for source_slot in source_resolution.slots:
        for dest_slot in dest_resolution.slots:
            if source_slot.memory_type == "TXT" and dest_slot.memory_type in allowed_dest_banks:
                continue
            location_text = _format_location(dest_location)
            findings.append(
                ClickFinding(
                    code=CLK_PACK_TEXT_BANK_INCOMPATIBLE,
                    severity=_route_severity(CLK_PACK_TEXT_BANK_INCOMPATIBLE, mode),
                    message=(
                        "pack_text is incompatible for "
                        f"{source_slot.memory_type} -> {dest_slot.memory_type} at {location_text}."
                    ),
                    location=location_text,
                )
            )

    return findings


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
) -> list[ClickFinding]:
    instruction_type = type(instruction).__name__
    if instruction_type not in {"EventDrumInstruction", "TimeDrumInstruction"}:
        return []

    findings: list[ClickFinding] = []

    def check_role(
        value: Any,
        role: str,
        location: ProgramLocation,
        unresolved_reason: str,
    ) -> None:
        resolution = _resolve_operand_slots(value, tag_map)
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
                        message=(
                            f"Bank {_bank_label(slot)} is invalid for role {role} "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    outputs = getattr(instruction, "outputs", ())
    for idx, output in enumerate(outputs):
        location = _instruction_location(base_location, f"instruction.outputs[{idx}]")
        resolution = _resolve_operand_slots(output, tag_map)
        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "drum output bank unresolved"))
            continue
        for slot in resolution.slots:
            if not profile.is_writable(slot.memory_type, slot.address):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_NOT_WRITABLE,
                        severity=_route_severity(CLK_BANK_NOT_WRITABLE, mode),
                        message=(
                            f"Write target {_bank_label(slot)} is not writable for Click "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )
            if not profile.valid_for_role(slot.memory_type, "drum_output_bit"):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_WRONG_ROLE,
                        severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
                        message=(
                            "Bank "
                            f"{_bank_label(slot)} is invalid for role drum_output_bit at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    if instruction_type == "EventDrumInstruction":
        events = getattr(instruction, "events", ())
        for idx, event_condition in enumerate(events):
            event_location = _instruction_location(base_location, f"instruction.events[{idx}]")
            event_tags = _iter_condition_tags(event_condition)
            for event_tag in event_tags:
                check_role(
                    value=event_tag,
                    role="drum_event_condition",
                    location=event_location,
                    unresolved_reason="event condition bank unresolved",
                )

    if instruction_type == "TimeDrumInstruction":
        presets = getattr(instruction, "presets", ())
        for idx, preset in enumerate(presets):
            if isinstance(preset, Tag):
                location = _instruction_location(base_location, f"instruction.presets[{idx}]")
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED,
                        severity=_route_severity(CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED, mode),
                        message=(
                            "time_drum preset must be a literal integer for Click portability "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
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
                    "drum jump step must be a DS memory address for Click portability "
                    f"at {location_text}."
                ),
                location=location_text,
            )
        )

    return findings


_INT16_MAX = 32_767

_TIMER_INSTRUCTION_TYPES = frozenset({"OnDelayInstruction", "OffDelayInstruction"})


def _evaluate_timer_preset_overflow(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    instruction_type = type(instruction).__name__
    if instruction_type not in _TIMER_INSTRUCTION_TYPES:
        return []

    preset = getattr(instruction, "preset", None)
    if isinstance(preset, Tag) or preset is None:
        return []

    if preset <= _INT16_MAX:
        return []

    location = _instruction_location(base_location, "instruction.preset")
    location_text = _format_location(location)
    suggestion = _build_suggestion(CLK_TIMER_PRESET_OVERFLOW, None, tag_map)
    return [
        ClickFinding(
            code=CLK_TIMER_PRESET_OVERFLOW,
            severity=_route_severity(CLK_TIMER_PRESET_OVERFLOW, mode),
            message=(
                f"Timer preset {preset} exceeds Click INT range (max {_INT16_MAX}) "
                f"at {location_text}."
            ),
            location=location_text,
            suggestion=suggestion,
        )
    ]
