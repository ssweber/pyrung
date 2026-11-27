"""Automatically generated module split."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any, assert_never

from pyclickplc.banks import BANKS

from pyrung.core.tag import Tag, TagType

from ..operands import RangeArg, TagArg
from ..resolvers import resolve_block_range_tags_ctx
from .types import RegisterType, WordOrder

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext

# ---------------------------------------------------------------------------
# Click-specific helpers
# ---------------------------------------------------------------------------


def _is_valid_index(bank: str, index: int) -> bool:
    cfg = BANKS[bank]
    if cfg.valid_ranges is None:
        return cfg.min_addr <= index <= cfg.max_addr
    return any(lo <= index <= hi for lo, hi in cfg.valid_ranges)


def _addresses_for_count(bank: str, start: int, count: int) -> tuple[int, ...]:
    if count <= 0:
        raise ValueError("count must be >= 1")

    if not _is_valid_index(bank, start):
        raise ValueError(f"{bank} address {start} is out of range")

    cfg = BANKS[bank]
    if cfg.valid_ranges is None:
        end = start + count - 1
        if end > cfg.max_addr:
            raise ValueError(
                f"{count} {bank} addresses from {bank} address {start} run past "
                f"{bank} address {cfg.max_addr}, the last one. "
                "Use a shorter source or dest, or an earlier remote_start."
            )
        return tuple(range(start, end + 1))

    addresses = [start]
    current = start
    while len(addresses) < count:
        current += 1
        while current <= cfg.max_addr and not _is_valid_index(bank, current):
            current += 1
        if current > cfg.max_addr:
            raise ValueError(
                f"{count} {bank} addresses from {bank} address {start} run past the last "
                f"valid {bank} address. Use a shorter source or dest, or an earlier remote_start."
            )
        addresses.append(current)
    return tuple(addresses)


def _contiguous_runs(addresses: tuple[int, ...]) -> list[tuple[int, int, int]]:
    runs: list[tuple[int, int, int]] = []
    run_start_addr = addresses[0]
    run_start_idx = 0
    prev_addr = addresses[0]

    for idx, addr in enumerate(addresses[1:], start=1):
        if addr != prev_addr + 1:
            runs.append((run_start_addr, run_start_idx, idx))
            run_start_addr = addr
            run_start_idx = idx
        prev_addr = addr

    runs.append((run_start_addr, run_start_idx, len(addresses)))
    return runs


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _normalize_operand_tags(operand: TagArg | RangeArg, ctx: ScanContext) -> list[Tag]:
    match operand:
        case TagArg(tag=tag):
            return [tag]
        case RangeArg(span=span):
            return resolve_block_range_tags_ctx(span, ctx)
        case _:
            assert_never(operand)


def _operand_length(operand: TagArg | RangeArg) -> int:
    """Return how many tags *operand* names."""
    match operand:
        case TagArg():
            return 1
        case RangeArg(span=span):
            return len(tuple(span.addresses))
        case _:
            assert_never(operand)


def _normalize_operand_count(operand: TagArg | RangeArg, count: int | None) -> int:
    expected = _operand_length(operand)
    effective = expected if count is None else count
    if effective != expected:
        raise ValueError(
            f"count={effective} does not match the {expected} tag(s) in the source or dest. "
            f"Leave count out, or set it to {expected}."
        )
    return expected


def _validate_status_tags(
    *,
    busy: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    busy_name: str,
) -> None:
    if busy.type != TagType.BOOL:
        raise TypeError(f"{busy_name} tag '{busy.name}' must be BOOL")
    if success.type != TagType.BOOL:
        raise TypeError(f"success tag '{success.name}' must be BOOL")
    if error.type != TagType.BOOL:
        raise TypeError(f"error tag '{error.name}' must be BOOL")
    if exception_response.type not in {TagType.INT, TagType.DINT}:
        raise TypeError(f"exception_response tag '{exception_response.name}' must be INT or DINT")


# ---------------------------------------------------------------------------
# Raw Modbus value packing / unpacking
# ---------------------------------------------------------------------------


#: The shift of each character in a register, first character first.  A raw send or receive
#: of Char tags keeps the first character in the low byte.  pyrung's Modbus server packs TXT
#: the same way.  A CLICK 4.00 confirmed it (round b13): a Receive and a Send both keep the
#: first character in the low byte, with no word swap.
TXT_BYTE_SHIFTS = (0, 8)


def _registers_per_tag(tag_type: TagType, register_type: RegisterType) -> int:
    """Return the number of Modbus registers (or coils) consumed by one tag."""
    if register_type in {RegisterType.COIL, RegisterType.DISCRETE_INPUT}:
        return 1
    if tag_type in {TagType.DINT, TagType.REAL}:
        return 2
    return 1


def _calculate_register_count(tag_types: list[TagType], register_type: RegisterType) -> int:
    return sum(_registers_per_tag(tt, register_type) for tt in tag_types)


def _register_count(tag_types: list[TagType], register_type: RegisterType, call: str) -> int:
    """Return how many registers a raw send or receive of tags of *tag_types* moves.

    Each register holds two characters, one from each of two Char tags.  CLICK's dialogs
    refuse an odd count, so the count of Char tags must be even.
    """
    if register_type in {RegisterType.HOLDING, RegisterType.INPUT} and _all_char(tag_types):
        if len(tag_types) % 2:
            raise ValueError(
                f"{call}() of {len(tag_types)} Char tags to a raw {register_type.name} "
                "register has no CLICK form, because each register holds two characters. "
                "Use an even number of Char tags."
            )
        return len(tag_types) // 2
    return _calculate_register_count(tag_types, register_type)


def _all_char(tag_types: list[TagType]) -> bool:
    return bool(tag_types) and all(tag_type is TagType.CHAR for tag_type in tag_types)


def _preview_operand_tag_types(operand: TagArg | RangeArg, count: int) -> list[TagType]:
    """Get tag types from an operand without needing a ScanContext."""
    match operand:
        case TagArg(tag=tag):
            return [tag.type]
        case RangeArg(span=span):
            return [span.block.type] * count
        case _:
            assert_never(operand)


def _pack_values_to_registers(
    values: list[Any],
    tags: list[Tag],
    word_order: WordOrder,
    register_type: RegisterType,
) -> list[Any]:
    """Pack tag values into Modbus register (or coil) values for writing."""
    if register_type == RegisterType.COIL:
        return [bool(v) for v in values]
    if _all_char([tag.type for tag in tags]):
        return [
            sum(
                ord(ch) << shift
                for ch, shift in zip(values[i : i + 2], TXT_BYTE_SHIFTS, strict=True)
            )
            for i in range(0, len(values), 2)
        ]
    registers: list[int] = []
    for value, tag in zip(values, tags, strict=True):
        if tag.type == TagType.DINT:
            hi, lo = struct.unpack(">HH", struct.pack(">i", int(value)))
            if word_order == WordOrder.HIGH_LOW:
                registers.extend([hi, lo])
            else:
                registers.extend([lo, hi])
        elif tag.type == TagType.REAL:
            hi, lo = struct.unpack(">HH", struct.pack(">f", float(value)))
            if word_order == WordOrder.HIGH_LOW:
                registers.extend([hi, lo])
            else:
                registers.extend([lo, hi])
        else:
            registers.append(int(value))
    return registers


def _unpack_registers_to_values(
    registers: list[Any],
    tags: list[Tag],
    word_order: WordOrder,
    register_type: RegisterType,
) -> tuple[Any, ...]:
    """Unpack Modbus register (or coil) values into tag-typed values."""
    if register_type in {RegisterType.COIL, RegisterType.DISCRETE_INPUT}:
        return tuple(bool(v) for v in registers[: len(tags)])
    if _all_char([tag.type for tag in tags]):
        return tuple(
            chr((int(registers[i // 2]) >> TXT_BYTE_SHIFTS[i % 2]) & 0xFF) for i in range(len(tags))
        )
    values: list[Any] = []
    idx = 0
    for tag in tags:
        if tag.type in {TagType.DINT, TagType.REAL}:
            if word_order == WordOrder.HIGH_LOW:
                hi, lo = registers[idx], registers[idx + 1]
            else:
                lo, hi = registers[idx], registers[idx + 1]
            raw_bytes = struct.pack(">HH", hi, lo)
            if tag.type == TagType.DINT:
                (val,) = struct.unpack(">i", raw_bytes)
            else:
                (val,) = struct.unpack(">f", raw_bytes)
            values.append(val)
            idx += 2
        else:
            values.append(registers[idx])
            idx += 1
    return tuple(values)
