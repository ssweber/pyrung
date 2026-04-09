"""Operand resolution and location helpers for Click validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyclickplc.addresses import parse_address

from pyrung.core.copy_converters import CopyConverter
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation.walker import ProgramLocation

from .findings import CLK_BANK_UNRESOLVED, ClickFinding, ValidationMode, _route_severity

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap

_KNOWN_BANKS: frozenset[str] = frozenset(
    {
        "X",
        "Y",
        "C",
        "T",
        "CT",
        "SC",
        "DS",
        "DD",
        "DH",
        "DF",
        "XD",
        "YD",
        "TD",
        "CTD",
        "SD",
        "TXT",
    }
)


@dataclass(frozen=True)
class _ResolvedSlot:
    memory_type: str
    address: int | None


@dataclass(frozen=True)
class _OperandResolution:
    slots: tuple[_ResolvedSlot, ...] = ()
    unresolved: bool = False


def _format_location(loc: ProgramLocation) -> str:
    """Convert a ProgramLocation into a deterministic human-readable string."""
    if loc.scope == "subroutine":
        prefix = f"subroutine[{loc.subroutine}].rung[{loc.rung_index}]"
    else:
        prefix = f"main.rung[{loc.rung_index}]"

    for branch_idx in loc.branch_path:
        prefix += f".branch[{branch_idx}]"

    if loc.instruction_index is not None:
        prefix += f".instruction[{loc.instruction_index}]({loc.instruction_type})"

    return f"{prefix}.{loc.arg_path}"


def _instruction_location(base: ProgramLocation, arg_path: str) -> ProgramLocation:
    return ProgramLocation(
        scope=base.scope,
        subroutine=base.subroutine,
        rung_index=base.rung_index,
        branch_path=base.branch_path,
        instruction_index=base.instruction_index,
        instruction_type=base.instruction_type,
        arg_path=arg_path,
    )


def _resolve_pointer_memory_type(pointer_name: str, tag_map: TagMap) -> str | None:
    """Resolve a pointer tag name to its memory_type via mapped_slots()."""
    found_types: set[str] = set()
    for slot in tag_map.mapped_slots():
        if slot.logical_name == pointer_name:
            found_types.add(slot.memory_type)

    if len(found_types) == 1:
        return next(iter(found_types))
    return None


def _resolve_direct_tag(tag: Tag, tag_map: TagMap) -> _ResolvedSlot | None:
    try:
        mapped_address = tag_map.resolve(tag)
        memory_type, address = parse_address(mapped_address)
        return _ResolvedSlot(memory_type=memory_type, address=address)
    except (KeyError, TypeError, ValueError):
        pass

    try:
        memory_type, address = parse_address(tag.name)
    except ValueError:
        return None
    return _ResolvedSlot(memory_type=memory_type, address=address)


def _resolve_block_memory_type(block_name: str, tag_map: TagMap) -> str | None:
    entry = tag_map._block_entry_by_name(block_name)
    if entry is not None and entry.hardware_addresses:
        hardware_slot = entry.hardware.block[entry.hardware_addresses[0]]
        try:
            memory_type, _ = parse_address(hardware_slot.name)
            return memory_type
        except ValueError:
            return None

    if block_name in _KNOWN_BANKS:
        return block_name

    return None


def _unique_slots(slots: list[_ResolvedSlot]) -> tuple[_ResolvedSlot, ...]:
    seen: set[tuple[str, int | None]] = set()
    ordered: list[_ResolvedSlot] = []
    for slot in slots:
        key = (slot.memory_type, slot.address)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(slot)
    return tuple(ordered)


def _resolve_operand_slots(value: Any, tag_map: TagMap) -> _OperandResolution:
    if isinstance(value, ImmediateRef):
        return _resolve_operand_slots(value.value, tag_map)

    if isinstance(value, CopyConverter):
        return _OperandResolution()

    if isinstance(value, Tag):
        resolved = _resolve_direct_tag(value, tag_map)
        if resolved is None:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=(resolved,))

    if isinstance(value, BlockRange):
        slots: list[_ResolvedSlot] = []
        unresolved = False
        for tag in value.tags():
            resolved = _resolve_direct_tag(tag, tag_map)
            if resolved is None:
                unresolved = True
                continue
            slots.append(resolved)
        if unresolved:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=_unique_slots(slots))

    if isinstance(value, IndirectBlockRange):
        memory_type = _resolve_block_memory_type(value.block.name, tag_map)
        if memory_type is None:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=(_ResolvedSlot(memory_type=memory_type, address=None),))

    if isinstance(value, (IndirectRef, IndirectExprRef)):
        memory_type = _resolve_block_memory_type(value.block.name, tag_map)
        if memory_type is None:
            return _OperandResolution(unresolved=True)
        return _OperandResolution(slots=(_ResolvedSlot(memory_type=memory_type, address=None),))

    return _OperandResolution()


def _bank_label(slot: _ResolvedSlot) -> str:
    if slot.address is None:
        return slot.memory_type
    return f"{slot.memory_type}{slot.address}"


def _unresolved_finding(
    location: ProgramLocation, mode: ValidationMode, reason: str
) -> ClickFinding:
    location_text = _format_location(location)
    return ClickFinding(
        code=CLK_BANK_UNRESOLVED,
        severity=_route_severity(CLK_BANK_UNRESOLVED, mode),
        message=f"Bank resolution failed at {location_text}: {reason}.",
        location=location_text,
    )
