"""Automatically generated module split."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pyrung.core import Block, BlockRange, Tag


@dataclass(frozen=True)
class MappedSlot:
    """Public runtime slot metadata for a mapped logical/hardware pair."""

    hardware_address: str
    logical_name: str
    default: object
    memory_type: str
    address: int
    read_only: bool
    source: Literal["user", "system"]


@dataclass(frozen=True)
class OwnerInfo:
    """Reverse-lookup result: which structure/block owns a hardware address."""

    structure_name: str
    instance: int | None  # 1-based, or None for singleton (count=1)
    field: str | None  # field name, or None for plain block slot
    structure_type: str  # "named_array", "udt", or "block"


@dataclass(frozen=True)
class StructuredImport:
    """Structured metadata reconstructed during nickname import."""

    name: str
    kind: Literal["udt", "named_array"]
    runtime: object
    count: int
    stride: int | None


@dataclass(frozen=True)
class _TagEntry:
    logical: Tag
    hardware: Tag


@dataclass(frozen=True)
class _BlockEntry:
    logical: Block
    hardware: BlockRange
    logical_addresses: tuple[int, ...]
    hardware_addresses: tuple[int, ...]
    logical_to_hardware: dict[int, int]


@dataclass(frozen=True)
class _BlockImportSpec:
    name: str
    memory_type: str
    start_idx: int
    end_idx: int
    hardware_range: BlockRange
    hardware_addresses: tuple[int, ...]
    bg_color: str | None = None
