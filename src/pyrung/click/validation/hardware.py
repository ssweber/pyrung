"""Hardware profile checks for Click validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyclickplc.banks import BANKS

from pyrung.click.capabilities import CONVERTER_COMPATIBILITY
from pyrung.core.condition import Condition
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.instruction.conversions import (
    LITERAL,
    _render_text_from_numeric,
    copy_source_kind,
)
from pyrung.core.instruction.send_receive import is_modbus_status_tag
from pyrung.core.memory_block import _element_name, _store_block, block_room
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.time_mode import TimeUnit
from pyrung.core.validation.walker import ProgramLocation, _condition_children

from .findings import (
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_WRONG_ROLE,
    CLK_COPY_BANK_INCOMPATIBLE,
    CLK_COPY_CONVERTER_INCOMPATIBLE,
    CLK_DRUM_JUMP_STEP_TAG_REQUIRED,
    CLK_OUT_UNIT_UNSUPPORTED,
    CLK_PACK_TEXT_BANK_INCOMPATIBLE,
    CLK_TEXT_PAST_BANK,
    CLK_TEXT_PAST_BLOCK,
    CLK_TIMER_PAIR_MISMATCH,
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
    _ResolutionCache,
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
        ("TimeEventDrumInstruction", "current_step"),
        ("TimeEventDrumInstruction", "accumulator"),
        ("TimeEventDrumInstruction", "completion_flag"),
        ("ToggleInstruction", "target"),
        ("DebounceInstruction", "target"),
        ("FlasherInstruction", "target"),
        ("TimedInstruction", "target"),
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
    ("TimeEventDrumInstruction", "current_step"): "drum_current_step",
    ("TimeEventDrumInstruction", "accumulator"): "drum_accumulator",
    ("TimeEventDrumInstruction", "completion_flag"): "drum_completion_flag",
    ("TimeEventDrumInstruction", "jump_step"): "drum_jump_step",
    # The Out coil variants claim each phase's whole Timer, as on_delay does.
    ("DebounceInstruction", "on_done"): "timer_done_bit",
    ("DebounceInstruction", "on_acc"): "timer_accumulator",
    ("DebounceInstruction", "on_preset"): "timer_preset",
    ("DebounceInstruction", "off_done"): "timer_done_bit",
    ("DebounceInstruction", "off_acc"): "timer_accumulator",
    ("DebounceInstruction", "off_preset"): "timer_preset",
    ("FlasherInstruction", "on_done"): "timer_done_bit",
    ("FlasherInstruction", "on_acc"): "timer_accumulator",
    ("FlasherInstruction", "on_preset"): "timer_preset",
    ("FlasherInstruction", "off_done"): "timer_done_bit",
    ("FlasherInstruction", "off_acc"): "timer_accumulator",
    ("FlasherInstruction", "off_preset"): "timer_preset",
    ("TimedInstruction", "timer_done"): "timer_done_bit",
    ("TimedInstruction", "timer_acc"): "timer_accumulator",
    ("TimedInstruction", "on_preset"): "timer_preset",
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
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for candidate_type, field_name in _WRITE_TARGET_FIELDS:
        if instruction_type != candidate_type:
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = resolver.resolve_operand_slots(getattr(instruction, field_name))

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
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for (candidate_type, field_name), role in _ROLE_ASSIGNMENT_FIELDS.items():
        if instruction_type != candidate_type:
            continue

        value = getattr(instruction, field_name)
        if value is None:
            continue
        if (field_name == "jump_step" or field_name.endswith("preset")) and not isinstance(
            value, Tag
        ):
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = resolver.resolve_operand_slots(value)

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


# Coil instructions -> (pyrung verb, CLICK coil name).  Their target must be
# bit memory; toggle() and the Out variants already take only a Bool tag.
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


def _evaluate_copy_compatibility(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    profile: HardwareProfile,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__
    mapping = _COPY_COMPATIBILITY_FIELDS.get(instruction_type)
    if mapping is None:
        return findings

    operation, source_field, dest_field = mapping
    source_location = _instruction_location(base_location, f"instruction.{source_field}")
    dest_location = _instruction_location(base_location, f"instruction.{dest_field}")

    source_resolution = resolver.resolve_operand_slots(getattr(instruction, source_field))
    dest_resolution = resolver.resolve_operand_slots(getattr(instruction, dest_field))

    if source_resolution.unresolved:
        findings.append(_unresolved_finding(source_location, mode, "source bank unresolved"))
    if dest_resolution.unresolved:
        findings.append(_unresolved_finding(dest_location, mode, "destination bank unresolved"))
    if source_resolution.unresolved or dest_resolution.unresolved:
        return findings

    if not source_resolution.slots or not dest_resolution.slots:
        return findings

    source_memory_types = tuple(dict.fromkeys(slot.memory_type for slot in source_resolution.slots))
    dest_memory_types = tuple(dict.fromkeys(slot.memory_type for slot in dest_resolution.slots))

    for source_memory_type in source_memory_types:
        for dest_memory_type in dest_memory_types:
            if not profile.copy_compatible(operation, source_memory_type, dest_memory_type):
                location_text = _format_location(dest_location)
                findings.append(
                    ClickFinding(
                        code=CLK_COPY_BANK_INCOMPATIBLE,
                        severity=_route_severity(CLK_COPY_BANK_INCOMPATIBLE, mode),
                        message=(
                            f"Copy operation {operation} is incompatible for "
                            f"{source_memory_type} -> {dest_memory_type} "
                            f"at {location_text}."
                        ),
                        location=location_text,
                    )
                )

    # --- Converter / bank compatibility ---
    # A plain Copy across text converts on its own; the bank check above covers it.
    converter = getattr(instruction, "authored_convert", getattr(instruction, "convert", None))
    if isinstance(converter, CopyConverter):
        compat = CONVERTER_COMPATIBILITY.get(converter.mode)
        if compat is not None:
            valid_sources, valid_dests = compat
            for source_memory_type in source_memory_types:
                if source_memory_type not in valid_sources:
                    location_text = _format_location(source_location)
                    findings.append(
                        ClickFinding(
                            code=CLK_COPY_CONVERTER_INCOMPATIBLE,
                            severity=_route_severity(CLK_COPY_CONVERTER_INCOMPATIBLE, mode),
                            message=(
                                f"Converter to_{converter.mode} requires source bank in "
                                f"{sorted(valid_sources)}, got {source_memory_type} "
                                f"at {location_text}."
                            ),
                            location=location_text,
                            suggestion=_build_suggestion(
                                CLK_COPY_CONVERTER_INCOMPATIBLE, None, tag_map
                            ),
                        )
                    )
            for dest_memory_type in dest_memory_types:
                if dest_memory_type not in valid_dests:
                    location_text = _format_location(dest_location)
                    findings.append(
                        ClickFinding(
                            code=CLK_COPY_CONVERTER_INCOMPATIBLE,
                            severity=_route_severity(CLK_COPY_CONVERTER_INCOMPATIBLE, mode),
                            message=(
                                f"Converter to_{converter.mode} requires destination bank in "
                                f"{sorted(valid_dests)}, got {dest_memory_type} "
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
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    if type(instruction).__name__ != "PackTextInstruction":
        return []

    findings: list[ClickFinding] = []
    source_location = _instruction_location(base_location, "instruction.source_range")
    dest_location = _instruction_location(base_location, "instruction.dest")

    source_resolution = resolver.resolve_operand_slots(instruction.source_range)
    dest_resolution = resolver.resolve_operand_slots(instruction.dest)

    if source_resolution.unresolved:
        findings.append(_unresolved_finding(source_location, mode, "source bank unresolved"))
    if dest_resolution.unresolved:
        findings.append(_unresolved_finding(dest_location, mode, "destination bank unresolved"))
    if source_resolution.unresolved or dest_resolution.unresolved:
        return findings

    if not source_resolution.slots or not dest_resolution.slots:
        return findings

    allowed_dest_banks = {"DS", "DD", "DH", "DF", "TD", "CTD"}
    source_memory_types = tuple(dict.fromkeys(slot.memory_type for slot in source_resolution.slots))
    dest_memory_types = tuple(dict.fromkeys(slot.memory_type for slot in dest_resolution.slots))
    for source_memory_type in source_memory_types:
        for dest_memory_type in dest_memory_types:
            if source_memory_type == "TXT" and dest_memory_type in allowed_dest_banks:
                continue
            location_text = _format_location(dest_location)
            findings.append(
                ClickFinding(
                    code=CLK_PACK_TEXT_BANK_INCOMPATIBLE,
                    severity=_route_severity(CLK_PACK_TEXT_BANK_INCOMPATIBLE, mode),
                    message=(
                        "pack_text is incompatible for "
                        f"{source_memory_type} -> {dest_memory_type} at {location_text}."
                    ),
                    location=location_text,
                )
            )

    return findings


# The fewest characters ``to_text()`` writes of a tag of each type, and whether that
# is every value's length: zero suppressed, one or more; else an Int's five digits
# (six with a sign), a Dint's ten (eleven), a Word's four hex digits.  A finite Real
# has seven decimals, "0.0000000" or "0.0000000E+00" in exponential form, the
# shortest; NaN and infinity are "nan", "inf" and "-inf".
_FIXED_TEXT_WIDTH = {TagType.INT: (5, False), TagType.DINT: (10, False), TagType.WORD: (4, True)}
_REAL_TEXT_WIDTH = 9
_REAL_EXPONENTIAL_TEXT_WIDTH = 13


@dataclass(frozen=True)
class _Written:
    """How many elements a Copy writes from its destination on.

    *count* is every value's length when *exact*, else the fewest, for a finite
    value only when *finite_only*.
    """

    count: int
    exact: bool
    finite_only: bool = False

    def describe(self, what: str) -> str:
        if self.exact:
            return f"{self.count} {what}"
        finite = " for a finite value" if self.finite_only else ""
        return f"{self.count} {what} or more{finite}"

    def dropped(self, room: int) -> str:
        if self.exact:
            return f"the last {self.count - room}"
        return f"all but the first {room}"

    def kept(self) -> str:
        return "every one" if self.exact else f"the first {self.count}"


def _fewest_elements_written(instruction: Any) -> _Written | None:
    """How many elements a Copy writes from its destination on, or ``None`` for one."""
    source = instruction.source
    converter = instruction.convert
    if converter is None:
        target = instruction.target
        is_text = isinstance(target, Tag) and target.type is TagType.CHAR
        return _Written(len(source), True) if isinstance(source, str) and is_text else None
    if converter.mode in {"value", "ascii"}:
        return _Written(len(source), True) if isinstance(source, str) else None
    if converter.mode != "text":
        return None
    tail = 0 if converter.termination_code is None else 1
    kind = copy_source_kind(source)
    if kind == LITERAL:
        try:
            text = _render_text_from_numeric(
                source,
                source_tag=None,
                suppress_zero=converter.suppress_zero,
                exponential=converter.exponential,
            )
        except (TypeError, ValueError, OverflowError):
            return None
        return _Written(len(text) + tail, True)
    if kind is TagType.REAL:
        width = _REAL_EXPONENTIAL_TEXT_WIDTH if converter.exponential else _REAL_TEXT_WIDTH
        return _Written(width + tail, False, finite_only=True)
    if converter.suppress_zero:
        return _Written(1 + tail, False)
    fixed = _FIXED_TEXT_WIDTH.get(kind)
    return None if fixed is None else _Written(fixed[0] + tail, fixed[1])


def _evaluate_text_past_bank(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """CLK_TEXT_PAST_BANK and CLK_TEXT_PAST_BLOCK: a Copy by address whose text runs
    past the end of its bank, or of its own block while the bank goes on.

    pyrung writes the elements its block holds and drops the rest.  Past the bank
    it warns, as the store is one a CLICK takes; past a block mapped inside a bank
    it warns that a CLICK writes on into the bank where pyrung stops.  Only a store
    whose shortest text runs past is reported.
    """
    if type(instruction).__name__ != "CopyInstruction":
        return []
    target = instruction.target
    if isinstance(target, ImmediateRef):
        target = target.value
    if not isinstance(target, Tag):
        return []
    written = _fewest_elements_written(instruction)
    if written is None or written.count < 2:
        return []
    resolution = resolver.resolve_operand_slots(target)
    if resolution.unresolved or len(resolution.slots) != 1:
        return []
    slot = resolution.slots[0]
    bank = BANKS.get(slot.memory_type)
    if bank is None or slot.address is None:
        return []
    count = written.count
    bank_room = bank.max_addr - slot.address + 1
    block_room_ = block_room(target)
    location_text = _format_location(_instruction_location(base_location, "instruction.target"))
    memory_type = slot.memory_type
    start = f"{memory_type}{slot.address}"
    what = "characters" if memory_type == "TXT" else "elements"
    severity = "error" if mode == "strict" else "warning"

    if block_room_ is not None and block_room_ < min(count, bank_room):
        home = _store_block(target)
        assert home is not None
        block = home[0]
        named = target.name if target.name == start else f"{target.name} ({start})"
        lowest = block.end - count + 1
        return [
            ClickFinding(
                code=CLK_TEXT_PAST_BLOCK,
                severity=severity,
                message=(
                    f"copy() into {named} at {location_text} writes "
                    f"{written.describe(what)}, past {_element_name(block, block.end)} at the "
                    f"end of its block {block.name}: pyrung drops "
                    f"{written.dropped(block_room_)}, where a "
                    f"CLICK writes on into {memory_type}{slot.address + block_room_}."
                ),
                location=location_text,
                suggestion=(
                    f"Start it at {_element_name(block, lowest)} or lower to keep "
                    f"{written.kept()}."
                    if lowest >= block.start
                    else f"Map {block.name} onto more of {memory_type} to keep {written.kept()}."
                ),
            )
        ]
    if count <= bank_room:
        return []
    lowest = bank.max_addr - count + 1
    return [
        ClickFinding(
            code=CLK_TEXT_PAST_BANK,
            severity=severity,
            message=(
                f"copy() into {start} at {location_text} writes {written.describe(what)}, "
                f"past {memory_type}{bank.max_addr} at the end of the bank: "
                f"{written.dropped(bank_room)} are dropped."
            ),
            location=location_text,
            suggestion=(
                f"Start it at {memory_type}{lowest} or lower to keep {written.kept()}."
                if lowest >= bank.min_addr
                else None
            ),
        )
    ]


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
        resolution = resolver.resolve_operand_slots(output)
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
                    unresolved_reason="event condition bank unresolved",
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

_TIMER_PRESET_FIELDS: dict[str, tuple[str, ...]] = {
    "OnDelayInstruction": ("preset",),
    "OffDelayInstruction": ("preset",),
    "DebounceInstruction": ("on_preset", "off_preset"),
    "FlasherInstruction": ("on_preset", "off_preset"),
    "TimedInstruction": ("on_preset",),
}


def _evaluate_timer_preset_overflow(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    for field_name in _TIMER_PRESET_FIELDS.get(type(instruction).__name__, ()):
        preset = getattr(instruction, field_name, None)
        if isinstance(preset, Tag) or preset is None or preset <= _INT16_MAX:
            continue
        location = _instruction_location(base_location, f"instruction.{field_name}")
        location_text = _format_location(location)
        findings.append(
            ClickFinding(
                code=CLK_TIMER_PRESET_OVERFLOW,
                severity=_route_severity(CLK_TIMER_PRESET_OVERFLOW, mode),
                message=(
                    f"Timer preset {preset} exceeds Click INT range (max {_INT16_MAX}) "
                    f"at {location_text}."
                ),
                location=location_text,
                suggestion=_build_suggestion(CLK_TIMER_PRESET_OVERFLOW, None, tag_map),
            )
        )
    return findings


# A CLICK timer or counter is one number: its instruction names T<n> (or CT<n>)
# and the accumulator TD<n> (CTD<n>) comes with it.  The Out coils name only TD<n>.
_TIMER_PAIR_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "OnDelayInstruction": (("done_bit", "accumulator"),),
    "OffDelayInstruction": (("done_bit", "accumulator"),),
    "CountUpInstruction": (("done_bit", "accumulator"),),
    "CountDownInstruction": (("done_bit", "accumulator"),),
    "DebounceInstruction": (("on_done", "on_acc"), ("off_done", "off_acc")),
    "FlasherInstruction": (("on_done", "on_acc"), ("off_done", "off_acc")),
    "TimedInstruction": (("timer_done", "timer_acc"),),
}
_PAIRED_BANKS = {"T": "TD", "CT": "CTD"}


def _evaluate_timer_pairs(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    for done_field, acc_field in _TIMER_PAIR_FIELDS.get(type(instruction).__name__, ()):
        done, acc = getattr(instruction, done_field), getattr(instruction, acc_field)
        done_slots = resolver.resolve_operand_slots(done).slots
        acc_slots = resolver.resolve_operand_slots(acc).slots
        if len(done_slots) != 1 or len(acc_slots) != 1:
            continue
        done_slot, acc_slot = done_slots[0], acc_slots[0]
        if _PAIRED_BANKS.get(done_slot.memory_type) != acc_slot.memory_type:
            continue  # a wrong bank is a role finding
        if done_slot.address == acc_slot.address:
            continue
        location_text = _format_location(
            _instruction_location(base_location, f"instruction.{acc_field}")
        )
        findings.append(
            ClickFinding(
                code=CLK_TIMER_PAIR_MISMATCH,
                severity=_route_severity(CLK_TIMER_PAIR_MISMATCH, mode),
                message=(
                    f"{_bank_label(done_slot)} and {_bank_label(acc_slot)} are different "
                    f"CLICK timers or counters at {location_text}; Done and Acc must share "
                    "one number."
                ),
                location=location_text,
                suggestion="Map Done and Acc to the same number, e.g. T5 with TD5.",
            )
        )
    return findings


# CLICK v4's Out dialog offers fewer units than its timers do.
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


# The banks a Send or Receive may write, from CLICK's help pages for them (the MODBUS
# TCP and RTU pages agree).  Not probed on hardware: no conformance case runs them.
# SD is a Receive destination there whole, so this does not apply copy()'s writable
# SD subset.
_MODBUS_FLAG_BANKS = frozenset({"C"})
_MODBUS_EXCEPTION_BANKS = frozenset({"DS", "DD"})
_MODBUS_RECEIVE_DEST_BANKS = frozenset(
    {"Y", "C", "CTD", "DS", "DD", "DH", "DF", "SD", "TD", "YD", "TXT"}
)
_MODBUS_WRITE_BANKS: dict[tuple[str, str], frozenset[str]] = {
    ("ModbusReceiveInstruction", "dest"): _MODBUS_RECEIVE_DEST_BANKS,
    ("ModbusReceiveInstruction", "receiving"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "success"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "error"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "exception_response"): _MODBUS_EXCEPTION_BANKS,
    ("ModbusSendInstruction", "sending"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "success"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "error"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "exception_response"): _MODBUS_EXCEPTION_BANKS,
}


def _evaluate_modbus_banks(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for (candidate_type, field_name), banks in _MODBUS_WRITE_BANKS.items():
        if instruction_type != candidate_type:
            continue

        value = getattr(instruction, field_name)
        # A status bit left off is a hidden tag that never reaches the CLICK project.
        if isinstance(value, Tag) and is_modbus_status_tag(value.name):
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = resolver.resolve_operand_slots(value)
        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "mapping missing or ambiguous"))
            continue

        for slot in resolution.slots:
            if slot.memory_type in banks:
                continue
            location_text = _format_location(location)
            choices = ", ".join(sorted(banks))
            findings.append(
                ClickFinding(
                    code=CLK_BANK_WRONG_ROLE,
                    severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
                    message=(
                        f"Bank {_bank_label(slot)} is invalid for {field_name} "
                        f"at {location_text}; CLICK takes {choices}."
                    ),
                    location=location_text,
                )
            )

    return findings
