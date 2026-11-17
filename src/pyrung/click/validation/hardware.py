"""Hardware profile checks for Click validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyclickplc.banks import BANKS

from pyrung.click.capabilities import (
    CONVERTER_COMPATIBILITY,
    FIELD_WRITABLE,
    HEX_CONSTANT_BANKS,
    single_register_fill_dest,
)
from pyrung.core.condition import Condition
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.instruction.conversions import (
    LITERAL,
    _render_text_from_numeric,
    copy_source_kind,
)
from pyrung.core.instruction.send_receive import is_modbus_status_tag
from pyrung.core.memory_block import _element_name, _store_block, block_room
from pyrung.core.rung import dsl_call
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.time_mode import TimeUnit
from pyrung.core.validation.walker import ProgramLocation, _condition_children

from .findings import (
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_WRONG_ROLE,
    CLK_COPY_BANK_INCOMPATIBLE,
    CLK_COPY_CONSTANT_INTO_BIT,
    CLK_COPY_CONVERTER_INCOMPATIBLE,
    CLK_COPY_WIDE_CONSTANT,
    CLK_DRUM_JUMP_STEP_TAG_REQUIRED,
    CLK_MATH_DEST_BANK,
    CLK_OUT_UNIT_UNSUPPORTED,
    CLK_PACK_TEXT_BANK_INCOMPATIBLE,
    CLK_SEARCH_RANGE_BANK,
    CLK_SEARCH_RESULT_NOT_DS,
    CLK_TEXT_PAST_BANK,
    CLK_TEXT_PAST_BLOCK,
    CLK_TIMER_PAIR_MISMATCH,
    CLK_TIMER_PRESET_OVERFLOW,
    CLK_TIMER_PRESET_TOO_LOW,
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
    _wrong_role_text,
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
        # A Fill of one register exports as a Copy, so the Copy's write rules apply.
        written_by = (instruction_type, field_name)
        if (
            instruction_type == "FillInstruction"
            and single_register_fill_dest(instruction.dest) is not None
        ):
            written_by = ("CopyInstruction", "target")

        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "operand"))
            continue

        for slot in resolution.slots:
            if slot.address is None and slot.memory_type in {"SC", "SD"}:
                findings.append(_unresolved_finding(location, mode, "SC or SD write target"))
                continue

            if not profile.field_writable(*written_by, slot.memory_type, slot.address):
                location_text = _format_location(location)
                findings.append(
                    ClickFinding(
                        code=CLK_BANK_NOT_WRITABLE,
                        severity=_route_severity(CLK_BANK_NOT_WRITABLE, mode),
                        message=_not_writable_text(instruction, slot, location_text, written_by),
                        location=location_text,
                    )
                )

    return findings


def _not_writable_text(
    instruction: Any,
    slot: Any,
    location_text: str,
    written_by: tuple[str, str] | None = None,
) -> str:
    """Return the message for a write that CLICK refuses to the instruction that makes it.

    CLICK lets some instructions write a bank that others cannot, such as out() on SC162.
    The advice names those instructions, read from ``FIELD_WRITABLE``.
    """
    call = dsl_call(instruction)
    others: list[str] = []
    for key, banks in FIELD_WRITABLE.items():
        if key == written_by or slot.memory_type not in banks:
            continue
        subset = banks[slot.memory_type]
        if subset is None or (slot.address is not None and slot.address in subset):
            other = dsl_call(key[0])
            if other != call and other not in others:
                others.append(other)
    if not others:
        advice = "Write to another address."
    else:
        listed = others[0] if len(others) == 1 else f"{', '.join(others[:-1])} or {others[-1]}"
        # out() drops its bit when the rung goes false, so it cannot stand in for a latch.
        if call in _HELD_COILS:
            advice = f"Only {listed} can write it."
        else:
            advice = f"Use {listed} instead."
    return f"CLICK does not let {call} write {_bank_label(slot)}, at {location_text}. {advice}"


_HELD_COILS = frozenset({"latch()", "reset()"})


def _role_words(role: str) -> str:
    """Return a role id such as ``timer_done_bit`` as the words ``timer done bit``."""
    return role.replace("_", " ")


def _role_banks(profile: HardwareProfile, role: str) -> str:
    """Return the banks that *profile* allows for *role*, as ``"Y, C"``."""
    return ", ".join(bank for bank in BANKS if profile.valid_for_role(bank, role))


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
            findings.append(_unresolved_finding(location, mode, "operand"))
            continue

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

    return findings


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


_BIT_BANKS = frozenset({"X", "Y", "C", "T", "CT", "SC"})
# The copy operations whose source may be a constant, with the CLICK instruction's name.
_CONSTANT_COPY_VERBS = {"single": ("copy", "Copy"), "fill": ("fill", "Fill")}


def _constant_into_bit(
    operation: str,
    value: Any,
    dest_memory_types: tuple[str, ...],
    dest_location: ProgramLocation,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Report CLK_COPY_CONSTANT_INTO_BIT, a constant copied or filled into bit memory.

    CLICK's Copy and Fill dialogs refuse it (checked in the v4 editor, 2026-11-06).
    """
    bits = [memory_type for memory_type in dest_memory_types if memory_type in _BIT_BANKS]
    if not bits:
        return []
    verb, box = _CONSTANT_COPY_VERBS[operation]
    if isinstance(value, ImmediateRef):
        value = value.value
    location_text = _format_location(dest_location)
    return [
        ClickFinding(
            code=CLK_COPY_CONSTANT_INTO_BIT,
            severity=_route_severity(CLK_COPY_CONSTANT_INTO_BIT, mode),
            message=(
                f"{verb}({value!r}, ...) into {', '.join(bits)} at {location_text}: CLICK's "
                f"{box} takes no constant into bit memory."
            ),
            location=location_text,
            suggestion=(
                "Set or clear the bit with out(), latch() or reset(), or copy a bit into it."
                if operation == "single"
                else "Set or clear the bits with latch() or reset() on the range."
            ),
        )
    ]


def _exponential_from_an_integer(
    instruction: Any,
    source: Any,
    source_resolution: Any,
    source_location: ProgramLocation,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Report Exponential Numbering on a Copy whose source is not a Real.

    CLICK offers Exponential Numbering only for a Real source. A DF, a DF pointer and a
    Real constant are Real sources. This check runs before the early return for a
    constant, so it also refuses an integer constant.
    """
    converter = getattr(instruction, "authored_convert", getattr(instruction, "convert", None))
    if not (
        isinstance(converter, CopyConverter) and converter.mode == "text" and converter.exponential
    ):
        return []
    if copy_source_kind(source) == LITERAL:
        value = source.value if isinstance(source, ImmediateRef) else source
        if isinstance(value, float):
            return []
        got = [f"the constant {value!r}"]
    else:
        got = [
            memory_type
            for memory_type in dict.fromkeys(slot.memory_type for slot in source_resolution.slots)
            if memory_type != "DF"
        ]
    location_text = _format_location(source_location)
    return [
        ClickFinding(
            code=CLK_COPY_CONVERTER_INCOMPATIBLE,
            severity=_route_severity(CLK_COPY_CONVERTER_INCOMPATIBLE, mode),
            message=(
                "to_text(exponential=True) needs a Real source, such as a DF or a Real "
                f"constant. The source here is {item} at {location_text}."
            ),
            location=location_text,
            suggestion="Remove exponential=True, or use suppress_zero for an integer source.",
        )
        for item in got
    ]


def _wide_constant_into_hex(
    instruction_type: str,
    value: Any,
    dest_memory_types: tuple[str, ...],
    dest_location: ProgramLocation,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Report CLK_COPY_WIDE_CONSTANT, a constant too wide for a hex register.

    A CLICK hex constant is 0h..FFFFh. The export refuses a constant into DH, XD or YD
    outside -32768..65535. It writes -32768..-1 as its 16 bits.
    """
    if isinstance(value, ImmediateRef):
        value = value.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    if isinstance(value, float) and not value.is_integer():
        return []
    if -32768 <= value <= 65535:
        return []
    hex_banks = [
        memory_type for memory_type in dest_memory_types if memory_type in HEX_CONSTANT_BANKS
    ]
    if not hex_banks:
        return []
    verb = "fill" if instruction_type == "FillInstruction" else "copy"
    location_text = _format_location(dest_location)
    return [
        ClickFinding(
            code=CLK_COPY_WIDE_CONSTANT,
            severity=_route_severity(CLK_COPY_WIDE_CONSTANT, mode),
            message=(
                f"{verb}({value!r}, ...) into {', '.join(hex_banks)} at {location_text} has a "
                "constant that does not fit in 16 bits. A CLICK hex constant is 0h..FFFFh, so "
                "the export refuses it."
            ),
            location=location_text,
            suggestion="Write the constant as its 16-bit value, 0..65535 (0h..FFFFh).",
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
    # CLICK has no Fill of one register, so the export is a Copy. It must also pass the
    # Fill table, so a Fill that CLICK refuses is never accepted this way.
    operations = (operation,)
    if operation == "fill" and single_register_fill_dest(instruction.dest) is not None:
        operations = ("fill", "single")
        operation = "single"
    source_location = _instruction_location(base_location, f"instruction.{source_field}")
    dest_location = _instruction_location(base_location, f"instruction.{dest_field}")

    source_resolution = resolver.resolve_operand_slots(getattr(instruction, source_field))
    dest_resolution = resolver.resolve_operand_slots(getattr(instruction, dest_field))

    if source_resolution.unresolved:
        findings.append(_unresolved_finding(source_location, mode, "source"))
    if dest_resolution.unresolved:
        findings.append(_unresolved_finding(dest_location, mode, "destination"))
    if source_resolution.unresolved or dest_resolution.unresolved:
        return findings

    dest_memory_types = tuple(dict.fromkeys(slot.memory_type for slot in dest_resolution.slots))
    source = getattr(instruction, source_field)
    if operation in _CONSTANT_COPY_VERBS and copy_source_kind(source) == LITERAL:
        findings.extend(
            _constant_into_bit(operation, source, dest_memory_types, dest_location, mode)
        )
        findings.extend(
            _wide_constant_into_hex(
                instruction_type, source, dest_memory_types, dest_location, mode
            )
        )
    findings.extend(
        _exponential_from_an_integer(instruction, source, source_resolution, source_location, mode)
    )

    if not source_resolution.slots or not dest_resolution.slots:
        return findings

    source_memory_types = tuple(dict.fromkeys(slot.memory_type for slot in source_resolution.slots))

    for source_memory_type in source_memory_types:
        for dest_memory_type in dest_memory_types:
            refused = next(
                (
                    op
                    for op in operations
                    if not profile.copy_compatible(op, source_memory_type, dest_memory_type)
                ),
                None,
            )
            if refused is not None:
                location_text = _format_location(dest_location)
                findings.append(
                    ClickFinding(
                        code=CLK_COPY_BANK_INCOMPATIBLE,
                        severity=_route_severity(CLK_COPY_BANK_INCOMPATIBLE, mode),
                        message=(
                            f"CLICK cannot {dsl_call(instruction)} from "
                            f"{source_memory_type} to {dest_memory_type}, at {location_text}."
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
                                f"to_{converter.mode}() reads only from "
                                f"{', '.join(sorted(valid_sources))}. The source at "
                                f"{location_text} is {source_memory_type}."
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
                                f"to_{converter.mode}() writes only to "
                                f"{', '.join(sorted(valid_dests))}. The destination at "
                                f"{location_text} is {dest_memory_type}."
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
        findings.append(_unresolved_finding(source_location, mode, "source"))
    if dest_resolution.unresolved:
        findings.append(_unresolved_finding(dest_location, mode, "destination"))
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
                        f"pack_text() at {location_text} cannot pack {source_memory_type} "
                        f"into {dest_memory_type}. CLICK packs TXT into DS, DD, DH, DF, TD or "
                        "CTD."
                    ),
                    location=location_text,
                )
            )

    return findings


# The banks that CLICK v4's Search dialog accepts (checked in the editor, 2026-11-06).
_SEARCH_RANGE_BANKS = ("DS", "DD", "DH", "DF", "TD", "CTD", "TXT")
_SEARCH_RESULT_BANKS = ("DS",)


def _evaluate_search_banks(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report CLK_SEARCH_RANGE_BANK and CLK_SEARCH_RESULT_NOT_DS.

    The first is a Search range in a bank that CLICK's Search does not accept. The second
    is a Result that is not a DS. The write-target and bank checks report an unresolved
    operand.
    """
    if type(instruction).__name__ != "SearchInstruction":
        return []
    findings: list[ClickFinding] = []
    checks = (
        ("search_range", _SEARCH_RANGE_BANKS, CLK_SEARCH_RANGE_BANK),
        ("result", _SEARCH_RESULT_BANKS, CLK_SEARCH_RESULT_NOT_DS),
    )
    for field_name, allowed, code in checks:
        resolution = resolver.resolve_operand_slots(getattr(instruction, field_name))
        banks = list(dict.fromkeys(slot.memory_type for slot in resolution.slots))
        bad = [bank for bank in banks if bank not in allowed]
        if not bad:
            continue
        location_text = _format_location(
            _instruction_location(base_location, f"instruction.{field_name}")
        )
        if code == CLK_SEARCH_RANGE_BANK:
            message = (
                f"search() range in {', '.join(bad)} at {location_text}; CLICK's Search "
                f"takes a range in {', '.join(allowed)} only."
            )
            suggestion = "Copy the values into a range in one of those banks and search that."
        else:
            message = (
                f"search() result in {', '.join(bad)} at {location_text}; CLICK's Search "
                "takes a DS Result only."
            )
            suggestion = "Map the result to a DS address, and copy it on from there if needed."
        findings.append(
            ClickFinding(
                code=code,
                severity=_route_severity(code, mode),
                message=message,
                location=location_text,
                suggestion=suggestion,
            )
        )
    return findings


# The banks that CLICK v4's Math dialog accepts as its Result (its Address Picker,
# 2026-11-16). A decimal Math accepts DS, DD, DF, TD, CTD and the writable SD. A hex Math
# accepts DH and YD. The table needs no mode. A Result in the other mode's bank makes a
# mixed Math, and CLK_MATH_MODE_MIXED flags that (``infer_calc_mode`` in calc.py).
_MATH_DEST_BANKS = ("DS", "DD", "DF", "TD", "CTD", "SD", "DH", "YD")


def _evaluate_math_dest_bank(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report CLK_MATH_DEST_BANK, a math() into a bank that CLICK's Math Result rejects.

    Those banks are bit memory, XD and TXT. The write-target check reports an unresolved
    destination and an SD that CLICK does not let a program write.
    """
    if type(instruction).__name__ != "CalcInstruction":
        return []
    resolution = resolver.resolve_operand_slots(instruction.dest)
    banks = list(dict.fromkeys(slot.memory_type for slot in resolution.slots))
    bad = [bank for bank in banks if bank not in _MATH_DEST_BANKS]
    if not bad:
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.dest"))
    return [
        ClickFinding(
            code=CLK_MATH_DEST_BANK,
            severity=_route_severity(CLK_MATH_DEST_BANK, mode),
            message=(
                f"math() into {', '.join(bad)} at {location_text}; CLICK's Math takes a "
                "Result in DS, DD, DF, TD, CTD or SD, or DH or YD in a hex Math."
            ),
            location=location_text,
            suggestion=(
                "Store the result in a register of one of those banks, and test or copy "
                "it on from there."
            ),
        )
    ]


# The fewest characters that ``to_text()`` writes for a tag of each type, and whether
# that is the length of every value. With zero suppressed the count is one or more.
# Otherwise an Int is five digits (six with a sign), a Dint ten (eleven) and a Word four
# hex digits. A finite Real has seven decimals. Its shortest form is "0.0000000", or
# "0.0000000E+00" in exponential form. NaN and infinity are "nan", "inf" and "-inf".
_FIXED_TEXT_WIDTH = {TagType.INT: (5, False), TagType.DINT: (10, False), TagType.WORD: (4, True)}
_REAL_TEXT_WIDTH = 9
_REAL_EXPONENTIAL_TEXT_WIDTH = 13


@dataclass(frozen=True)
class _Written:
    """How many elements a Copy writes from its destination on.

    *count* is the length of every value when *exact*, and the fewest otherwise. It
    applies to a finite value only when *finite_only*.
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
        """Describe what a store with *room* elements left drops."""
        if self.exact:
            lost = self.count - room
            return "the last one" if lost == 1 else f"the last {lost}"
        return "all but the first" if room == 1 else f"all but the first {room}"

    def dropped_verb(self, room: int) -> str:
        return "is" if self.exact and self.count - room == 1 else "are"

    def kept(self) -> str:
        return "every one" if self.exact else f"the first {self.count}"


def _fewest_elements_written(instruction: Any) -> _Written | None:
    """Return how many elements a Copy writes from its destination on.

    Return ``None`` when it writes one.
    """
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


def _saved_constant_length(value: Any) -> int | None:
    """Return how many characters CLICK saves for a Copy's constant, without outer quotes.

    CLICK sets a text Copy's end address from this length, not from the text it writes
    (1.5 into TXT1 ends at TXT3). Text and a whole number count in the export's spelling,
    which is CLICK's saved form. A doubled quote counts twice, so "A""B" ends at TXT4
    (clicknick ``buildbot/jobs/lc-diff-click/RESULTS9-10.md``).

    A Real keeps at most 7 digits, so 3.4028235E+38 saves as 3.402823E+38. From 1e7 up,
    CLICK saves a Real in E form: 123456789.0 as 1.234568E+08 and 1e10 as 1E+10
    (RESULTS9-10). The export still spells those positionally, so this function counts
    CLICK's E form. No CLICK result covers the E form of a smaller Real, so a smaller
    Real counts in the export's spelling.
    """
    from pyrung.click.ladder.translator import _float32, click_real_literal

    if isinstance(value, ImmediateRef):
        value = value.value
    if isinstance(value, str):
        return len(value.replace('"', '""'))
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        text = click_real_literal(value)
        if text is None:
            return None
        f32 = _float32(value)
        if f32 is not None and abs(f32) >= 1e7:
            text = f"{f32:.7G}"
        return len(text)
    return None


def constant_text_past_txt(instruction: Any, address: int) -> int | None:
    """Return how many characters CLICK saves for a text Copy's constant into TXT *address*.

    Return ``None`` unless those characters run past TXT1000. CLICK's Copy dialog refuses
    that store (OBJ-003). CLICK's Copy accepts a register source, which saves no end
    address, so this function does not judge a register source.
    """
    if copy_source_kind(instruction.source) != LITERAL:
        return None
    if _fewest_elements_written(instruction) is None:
        return None
    length = _saved_constant_length(instruction.source)
    if length is None or address + length - 1 <= BANKS["TXT"].max_addr:
        return None
    return length


def _evaluate_text_past_bank(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report CLK_TEXT_PAST_BANK and CLK_TEXT_PAST_BLOCK.

    Each is a Copy by address whose text runs past the end of its bank, or past the end
    of its own block while the bank goes on.

    pyrung writes the elements that its block holds and drops the rest. A constant into
    TXT that runs past the bank as written is refused, because CLICK's Copy refuses it
    (OBJ-003). Any other store past the bank warns. CLICK's Copy accepts a register
    source, which saves no end address. A store past a block that is mapped inside a bank
    warns that CLICK writes on into the bank, where pyrung stops. Only a store whose
    shortest text runs past the end is reported.
    """
    if type(instruction).__name__ != "CopyInstruction":
        return []
    target = instruction.target
    if isinstance(target, ImmediateRef):
        target = target.value
    if not isinstance(target, Tag):
        return []
    written = _fewest_elements_written(instruction)
    if written is None:
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
    location_text = _format_location(_instruction_location(base_location, "instruction.target"))
    memory_type = slot.memory_type
    start = f"{memory_type}{slot.address}"
    severity = "error" if mode == "strict" else "warning"

    saved = constant_text_past_txt(instruction, slot.address) if memory_type == "TXT" else None
    if saved is not None:
        lowest = bank.max_addr - max(saved, count) + 1
        named = target.name if target.name == start else f"{target.name} ({start})"
        return [
            ClickFinding(
                code=CLK_TEXT_PAST_BANK,
                severity=severity,
                message=(
                    f"copy() into {named} at {location_text} has a {saved}-character "
                    f"constant, which runs past {memory_type}{bank.max_addr} at the end of the "
                    "bank. CLICK's Copy refuses it (OBJ-003), so the export refuses it."
                ),
                location=location_text,
                suggestion=(
                    f"Start it at {memory_type}{lowest} or lower to keep {written.kept()}."
                    if lowest >= bank.min_addr
                    else None
                ),
            )
        ]
    if count < 2:
        return []
    block_room_ = block_room(target)
    what = "characters" if memory_type == "TXT" else "elements"

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
                    f"Start it at {_element_name(block, lowest)} or lower to keep {written.kept()}."
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
                f"{written.dropped(bank_room)} {written.dropped_verb(bank_room)} dropped."
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


# The least constant preset that CLICK v4's dialogs accept (checked in the editor,
# 2026-11-06). A flasher or a timed coil refuses 0. A debounce and a drum step accept 0
# and refuse a negative one. A register preset accepts any value.
_CONSTANT_PRESET_FLOORS: dict[str, tuple[str, int, tuple[str, ...]]] = {
    "DebounceInstruction": ("debounce", 0, ("on_preset", "off_preset")),
    "FlasherInstruction": ("flasher", 1, ("on_preset", "off_preset")),
    "TimedInstruction": ("timed", 1, ("on_preset",)),
    "TimeDrumInstruction": ("time_drum", 0, ()),
    "TimeEventDrumInstruction": ("time_event_drum", 0, ()),
}


def _evaluate_timer_preset_floor(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Report CLK_TIMER_PRESET_TOO_LOW, a constant preset below what CLICK accepts."""
    spec = _CONSTANT_PRESET_FLOORS.get(type(instruction).__name__)
    if spec is None:
        return []
    verb, floor, fields = spec
    presets = (
        [(field, getattr(instruction, field)) for field in fields]
        if fields
        else [(f"presets[{index}]", p) for index, p in enumerate(instruction.presets)]
    )
    least = "1 or more" if floor == 1 else "0 or more"
    findings: list[ClickFinding] = []
    for field_name, preset in presets:
        if not isinstance(preset, int) or isinstance(preset, bool) or preset >= floor:
            continue
        location_text = _format_location(
            _instruction_location(base_location, f"instruction.{field_name}")
        )
        findings.append(
            ClickFinding(
                code=CLK_TIMER_PRESET_TOO_LOW,
                severity=_route_severity(CLK_TIMER_PRESET_TOO_LOW, mode),
                message=(
                    f"{verb}() preset {preset} at {location_text}: CLICK's editor takes a "
                    f"constant preset of {least} there."
                ),
                location=location_text,
                suggestion=(
                    f"Use a constant of {least}, or a DS tag holding the value: a register "
                    "preset takes any value."
                ),
            )
        )
    return findings


# A CLICK timer or counter is one number. Its instruction names T<n> (or CT<n>), and the
# accumulator TD<n> (CTD<n>) comes with it. The Out coils name only TD<n>.
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


# The banks that each Send or Receive operand accepts, from CLICK's help pages for them
# (the MODBUS TCP and RTU pages agree). No conformance case runs them, so they are not
# probed on hardware. The help pages accept the whole SD bank as a Receive destination,
# so this does not apply copy()'s writable SD subset.
_MODBUS_FLAG_BANKS = frozenset({"C"})
_MODBUS_EXCEPTION_BANKS = frozenset({"DS", "DD"})
_MODBUS_DEVICE_ID_BANKS = frozenset({"DS"})
_MODBUS_RECEIVE_DEST_BANKS = frozenset(
    {"Y", "C", "CTD", "DS", "DD", "DH", "DF", "SD", "TD", "YD", "TXT"}
)
_MODBUS_BANKS: dict[tuple[str, str], frozenset[str]] = {
    ("ModbusReceiveInstruction", "dest"): _MODBUS_RECEIVE_DEST_BANKS,
    ("ModbusReceiveInstruction", "receiving"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "success"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "error"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "exception_response"): _MODBUS_EXCEPTION_BANKS,
    ("ModbusSendInstruction", "sending"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "success"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "error"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "exception_response"): _MODBUS_EXCEPTION_BANKS,
    # CLICK's Slave ID box accepts a number or a DS address.
    ("ModbusReceiveInstruction", "device_id"): _MODBUS_DEVICE_ID_BANKS,
    ("ModbusSendInstruction", "device_id"): _MODBUS_DEVICE_ID_BANKS,
}


def _evaluate_modbus_banks(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for (candidate_type, field_name), banks in _MODBUS_BANKS.items():
        if instruction_type != candidate_type:
            continue

        value = getattr(instruction, field_name)
        # If the caller did not pass a status bit, pyrung gives it a hidden tag.  That tag
        # never reaches the CLICK project.
        if isinstance(value, Tag) and is_modbus_status_tag(value.name):
            continue
        # A numeric device ID has no bank.
        if isinstance(value, int):
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = resolver.resolve_operand_slots(value)
        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "operand"))
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
