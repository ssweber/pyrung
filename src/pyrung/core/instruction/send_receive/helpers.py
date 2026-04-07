"""Automatically generated module split."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any

from pyclickplc.banks import BANKS

from pyrung.core.memory_block import BlockRange
from pyrung.core.tag import Tag, TagType

from ..resolvers import resolve_block_range_tags_ctx, resolve_tag_ctx
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


def _range_end_for_count(bank: str, start: int, count: int) -> int:
    return _addresses_for_count(bank, start, count)[-1]


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
                f"{bank} range overflow: start {start}, count {count} exceeds {cfg.max_addr}"
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
                f"{bank} range overflow: start {start}, count {count} exceeds valid addresses"
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


def _normalize_operand_tags(operand: Tag | BlockRange, ctx: ScanContext) -> list[Tag]:
    if isinstance(operand, Tag):
        return [resolve_tag_ctx(operand, ctx)]
    return resolve_block_range_tags_ctx(operand, ctx)


def _normalize_operand_count(operand: Tag | BlockRange, count: int | None) -> int:
    expected = 1 if isinstance(operand, Tag) else len(tuple(operand.addresses))
    effective = expected if count is None else count
    if effective != expected:
        raise ValueError(
            f"count mismatch: operand resolves to {expected} tag(s) but count={effective}"
        )
    return expected


def _status_clear_tags(
    busy: Tag, success: Tag, error: Tag, exception_response: Tag
) -> dict[str, Any]:
    return {
        busy.name: False,
        success.name: False,
        error.name: False,
        exception_response.name: 0,
    }


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


def _registers_per_tag(tag_type: TagType, register_type: RegisterType) -> int:
    """Return the number of Modbus registers (or coils) consumed by one tag."""
    if register_type in {RegisterType.COIL, RegisterType.DISCRETE_INPUT}:
        return 1
    if tag_type in {TagType.DINT, TagType.REAL}:
        return 2
    return 1


def _calculate_register_count(tag_types: list[TagType], register_type: RegisterType) -> int:
    return sum(_registers_per_tag(tt, register_type) for tt in tag_types)


def _preview_operand_tag_types(operand: Tag | BlockRange, count: int) -> list[TagType]:
    """Get tag types from an operand without needing a ScanContext."""
    if isinstance(operand, Tag):
        return [operand.type]
    return [operand.block.type] * count


def _pack_values_to_registers(
    values: list[Any],
    tags: list[Tag],
    word_order: WordOrder,
    register_type: RegisterType,
) -> list[Any]:
    """Pack tag values into Modbus register (or coil) values for writing."""
    if register_type == RegisterType.COIL:
        return [bool(v) for v in values]
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
