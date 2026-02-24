"""Click logical-to-hardware mapping layer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile
    from pyrung.click.validation import ClickValidationReport, ValidationMode
    from pyrung.core.program import Program

import pyclickplc
from pyclickplc.addresses import AddressRecord, format_address_display, get_addr_key, parse_address
from pyclickplc.banks import BANKS, DEFAULT_RETENTIVE, MEMORY_TYPE_BASES, DataType
from pyclickplc.blocks import compute_all_block_ranges, format_block_tag
from pyclickplc.validation import validate_nickname

from pyrung.click.system_mappings import SYSTEM_CLICK_SLOTS
from pyrung.core import Block, BlockRange, InputBlock, OutputBlock, Tag, TagType
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import MappingEntry


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


_DATA_TYPE_TO_TAG_TYPE: dict[DataType, TagType] = {
    DataType.BIT: TagType.BOOL,
    DataType.INT: TagType.INT,
    DataType.INT2: TagType.DINT,
    DataType.FLOAT: TagType.REAL,
    DataType.HEX: TagType.WORD,
    DataType.TXT: TagType.CHAR,
}

_HARDWARE_BLOCK_CACHE: dict[str, Block | InputBlock | OutputBlock] = {}


def _tag_type_for_memory_type(memory_type: str) -> TagType:
    config = BANKS[memory_type]
    return _DATA_TYPE_TO_TAG_TYPE[config.data_type]


def _compress_addresses_to_ranges(addresses: list[int]) -> tuple[tuple[int, int], ...] | None:
    if not addresses:
        return None

    ranges: list[tuple[int, int]] = []
    lo = hi = addresses[0]
    for addr in addresses[1:]:
        if addr == hi + 1:
            hi = addr
            continue
        ranges.append((lo, hi))
        lo = hi = addr
    ranges.append((lo, hi))
    return tuple(ranges)


def _valid_ranges_for_bank(memory_type: str) -> tuple[tuple[int, int], ...] | None:
    config = BANKS[memory_type]
    if config.valid_ranges is not None:
        return config.valid_ranges
    if memory_type not in {"XD", "YD"}:
        return None

    # XD/YD expose a sparse MDB address set where XD0u/YD0u maps to address 1.
    valid_addresses: list[int] = []
    for addr in range(config.min_addr, config.max_addr + 1):
        display = format_address_display(memory_type, addr)
        try:
            parsed_bank, parsed_addr = parse_address(display)
        except ValueError:
            continue
        if parsed_bank == memory_type and parsed_addr == addr:
            valid_addresses.append(addr)
    return _compress_addresses_to_ranges(valid_addresses)


def _hardware_block_for(memory_type: str) -> Block | InputBlock | OutputBlock:
    cached = _HARDWARE_BLOCK_CACHE.get(memory_type)
    if cached is not None:
        return cached

    config = BANKS[memory_type]
    name = config.name
    tag_type = _tag_type_for_memory_type(config.name)
    start = config.min_addr
    end = config.max_addr
    valid_ranges = _valid_ranges_for_bank(memory_type)
    formatter = format_address_display

    if memory_type in {"X", "XD"}:
        block = InputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=formatter,
        )
    elif memory_type in {"Y", "YD"}:
        block = OutputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=formatter,
        )
    else:
        block = Block(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            retentive=DEFAULT_RETENTIVE[memory_type],
            valid_ranges=valid_ranges,
            address_formatter=formatter,
        )
    _HARDWARE_BLOCK_CACHE[memory_type] = block
    return block


def _parse_default(initial_value: str, tag_type: TagType) -> object:
    if initial_value == "":
        if tag_type == TagType.BOOL:
            return False
        if tag_type in (TagType.INT, TagType.DINT, TagType.WORD):
            return 0
        if tag_type == TagType.REAL:
            return 0.0
        if tag_type == TagType.CHAR:
            return ""
        return 0

    try:
        if tag_type == TagType.BOOL:
            return initial_value == "1"
        if tag_type in (TagType.INT, TagType.DINT):
            return int(initial_value)
        if tag_type == TagType.REAL:
            return float(initial_value)
        if tag_type == TagType.WORD:
            return int(initial_value, 16)
        if tag_type == TagType.CHAR:
            return initial_value[:1]
    except ValueError:
        pass

    if tag_type == TagType.REAL:
        return 0.0
    if tag_type == TagType.CHAR:
        return ""
    if tag_type == TagType.BOOL:
        return False
    return 0


def _format_default(value: object, tag_type: TagType) -> str:
    if tag_type == TagType.BOOL:
        return "1" if bool(value) else "0"
    if tag_type in (TagType.INT, TagType.DINT):
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(int(value))
        if isinstance(value, str):
            try:
                return str(int(value))
            except ValueError:
                return "0"
        return "0"
    if tag_type == TagType.REAL:
        if isinstance(value, bool):
            return "1.0" if value else "0.0"
        if isinstance(value, (int, float)):
            return str(float(value))
        if isinstance(value, str):
            try:
                return str(float(value))
            except ValueError:
                return "0.0"
        return "0.0"
    if tag_type == TagType.WORD:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return f"{value:X}"
        if isinstance(value, float):
            return f"{int(value):X}"
        if isinstance(value, str):
            try:
                return f"{int(value, 16):X}"
            except ValueError:
                try:
                    return f"{int(value):X}"
                except ValueError:
                    return "0"
        return "0"
    if tag_type == TagType.CHAR:
        if not isinstance(value, str):
            return ""
        return value[:1]
    return str(value)


class TagMap:
    """Maps logical Tags and Blocks to Click hardware addresses.

    `TagMap` is the pivot of the Click dialect.  It links semantic tags (which
    have no hardware knowledge) to concrete Click addresses, and drives
    nickname file round-trips and validation.

    **Constructing from a dict** — map individual tags and entire blocks:

    .. code-block:: python

        from pyrung.click import TagMap, x, y, c, ds

        mapping = TagMap({
            StartButton:  x[1],              # Tag → Tag (BOOL → X001)
            Motor:        y[1],              # Tag → Tag (BOOL → Y001)
            Alarms:       c.select(1, 100),  # Block → BlockRange
            Speed:        ds[1],             # Tag → Tag (INT → DS1)
        })

    **From a Click nickname CSV file:**

    .. code-block:: python

        mapping = TagMap.from_nickname_file("project.csv")

    **Exporting back to CSV:**

    .. code-block:: python

        mapping.to_nickname_file("project.csv")

    **Validating a program:**

    .. code-block:: python

        report = mapping.validate(logic, mode="warn")
        print(report.summary())

    **Resolving a logical tag to its hardware address:**

    .. code-block:: python

        mapping.resolve(Speed)              # "DS1"
        mapping.resolve(Alarms, index=5)   # "C5"

    Type compatibility is validated at construction time — mapping a BOOL tag
    to a DS address (INT) raises ``ValueError``.  Hardware address conflicts
    (two logical tags mapped to the same Click address) also raise
    ``ValueError``.

    Args:
        mappings: ``dict[Tag | Block, Tag | BlockRange]``,
            ``Iterable[MappingEntry]``, or ``None`` for an empty map.
        include_system: Whether to include built-in system tag mappings
            (SC/SD points). Default ``True``.
    """

    def __init__(
        self,
        mappings: dict[Tag | Block, Tag | BlockRange] | Iterable[MappingEntry] | None = None,
        *,
        include_system: bool = True,
    ):
        normalized = self._normalize_mappings(mappings)
        self._include_system = include_system

        self._tag_entries: list[_TagEntry] = []
        self._block_entries: list[_BlockEntry] = []
        self._entries: list[_TagEntry | _BlockEntry] = []
        self._system_tag_entries: list[_TagEntry] = []
        self._tag_forward: dict[str, _TagEntry] = {}
        self._system_tag_forward: dict[str, _TagEntry] = {}
        self._system_alias_forward: dict[str, _TagEntry] = {}
        self._system_read_only: dict[str, bool] = {}
        self._block_lookup: dict[int, _BlockEntry] = {}
        self._block_by_name: dict[str, _BlockEntry] = {}
        self._user_logical_names: set[str] = set()
        self._block_slot_forward_by_id: dict[int, Tag] = {}
        self._block_slot_forward_by_name: dict[str, Tag] = {}
        self._warnings: tuple[str, ...] = ()

        used_hardware: dict[int, str] = {}
        used_hardware_logical: dict[int, str] = {}

        for mapping in normalized:
            source = mapping.source
            target = mapping.target

            if isinstance(source, Tag) and isinstance(target, Tag):
                self._reject_reserved_system_name(source.name)
                self._validate_tag_mapping(source, target)
                self._register_user_logical_name(
                    source.name, owner=f"standalone logical tag {source.name!r}"
                )

                memory_type, address = self._parse_hardware_tag(target)
                self._claim_hardware_address(
                    get_addr_key(memory_type, address),
                    owner=f"tag {source.name!r}",
                    logical_name=source.name,
                    used_hardware=used_hardware,
                    used_hardware_logical=used_hardware_logical,
                )

                entry = _TagEntry(logical=source, hardware=target)
                self._tag_entries.append(entry)
                self._entries.append(entry)
                self._tag_forward[source.name] = entry
                continue

            if isinstance(source, Block) and isinstance(target, BlockRange):
                block_id = id(source)
                if block_id in self._block_lookup:
                    raise ValueError(f"Logical block {source.name!r} is already mapped.")

                block_entry = self._build_block_entry(source, target)
                if len(block_entry.logical_addresses) > len(block_entry.hardware_addresses):
                    raise ValueError(
                        f"Block size mismatch: logical block {source.name!r} has "
                        f"{len(block_entry.logical_addresses)} slots but hardware range has "
                        f"{len(block_entry.hardware_addresses)} slots."
                    )

                self._validate_block_mapping(block_entry)

                for logical_addr, hardware_addr in block_entry.logical_to_hardware.items():
                    logical_slot = source[logical_addr]
                    self._reject_reserved_system_name(logical_slot.name)
                    self._register_user_logical_name(
                        logical_slot.name,
                        owner=f"block slot {source.name}[{logical_addr}]",
                    )
                    memory_type, _ = self._parse_hardware_tag(
                        block_entry.hardware.block[hardware_addr]
                    )
                    self._claim_hardware_address(
                        get_addr_key(memory_type, hardware_addr),
                        owner=f"block {source.name!r}",
                        logical_name=logical_slot.name,
                        used_hardware=used_hardware,
                        used_hardware_logical=used_hardware_logical,
                    )

                self._block_entries.append(block_entry)
                self._entries.append(block_entry)
                self._block_lookup[block_id] = block_entry
                self._block_by_name[source.name] = block_entry
                for logical_addr, hardware_addr in block_entry.logical_to_hardware.items():
                    logical_slot = source[logical_addr]
                    hardware_slot = block_entry.hardware.block[hardware_addr]
                    self._block_slot_forward_by_id[id(logical_slot)] = hardware_slot
                    self._block_slot_forward_by_name[logical_slot.name] = hardware_slot
                continue

            raise ValueError(
                "Unsupported mapping pair. Supported mappings are Tag->Tag and Block->BlockRange."
            )

        if self._include_system:
            for slot in SYSTEM_CLICK_SLOTS:
                memory_type, address = self._parse_hardware_tag(slot.hardware)
                self._claim_hardware_address(
                    get_addr_key(memory_type, address),
                    owner=f"system tag {slot.logical.name!r}",
                    logical_name=slot.logical.name,
                    used_hardware=used_hardware,
                    used_hardware_logical=used_hardware_logical,
                    compatible_logical_names={slot.logical.name, slot.click_nickname},
                )
                entry = _TagEntry(logical=slot.logical, hardware=slot.hardware)
                self._system_tag_entries.append(entry)
                self._system_tag_forward[slot.logical.name] = entry
                self._system_alias_forward[slot.click_nickname] = entry
                self._system_read_only[slot.logical.name] = slot.read_only

        self._freeze_entries()
        self._refresh_nickname_validation()

    @classmethod
    def from_nickname_file(cls, path: str | Path) -> TagMap:
        """Build a `TagMap` from a Click nickname CSV file.

        Reads the CSV produced by Click Programming Software and reconstructs
        logical-to-hardware mappings:

        - **Block tag pairs** (rows with ``<Name>`` / ``</Name>`` comments) →
          ``Block`` objects mapped to hardware ranges.
        - **Standalone nicknames** → individual ``Tag`` objects.
        - ``_D`` suffix pairs (timer/counter accumulators) are linked
          automatically.
        - Initial values and retentive flags are preserved.

        Args:
            path: Path to the Click nickname CSV file.

        Returns:
            A `TagMap` ready for use with `validate()` and `to_nickname_file()`.

        Raises:
            FileNotFoundError: If the path does not exist.
            ValueError: If the CSV contains conflicting block boundaries or
                mismatched memory types.
        """
        records = pyclickplc.read_csv(path)
        rows = sorted(
            records.values(),
            key=lambda row: (MEMORY_TYPE_BASES[row.memory_type], row.address),
        )
        ranges = compute_all_block_ranges(cast(list, rows))

        mappings: list[MappingEntry] = []
        seen_names: dict[str, tuple[str, int]] = {}
        covered_rows: set[int] = set()

        def require_representable_block_nickname(*, memory_type: str, address: int, name: str) -> None:
            display = format_address_display(memory_type, address)
            if name == "":
                raise ValueError(
                    f"Block row nickname at {display} cannot be blank; a representable slot "
                    "name must be non-empty."
                )
            is_valid, error = validate_nickname(name)
            if not is_valid:
                raise ValueError(
                    f"Block row nickname at {display} is not representable: {error}."
                )
            existing = seen_names.get(name)
            if existing is not None and existing != (memory_type, address):
                existing_display = format_address_display(existing[0], existing[1])
                raise ValueError(
                    f"Block row nickname at {display} is not representable: duplicate logical "
                    f"name {name!r} already used at {existing_display}."
                )
            seen_names[name] = (memory_type, address)

        for block_range in ranges:
            covered_rows.update(range(block_range.start_idx, block_range.end_idx + 1))
            start_row = rows[block_range.start_idx]
            end_row = rows[block_range.end_idx]
            memory_type = start_row.memory_type
            if end_row.memory_type != memory_type:
                raise ValueError(
                    f"Block {block_range.name!r} spans multiple memory types: "
                    f"{memory_type} and {end_row.memory_type}."
                )

            start_addr = min(start_row.address, end_row.address)
            end_addr = max(start_row.address, end_row.address)
            hardware_block = _hardware_block_for(memory_type)
            hardware_range = hardware_block.select(start_addr, end_addr)
            hardware_addresses = tuple(hardware_range.addresses)
            if not hardware_addresses:
                continue

            logical_block = Block(
                name=block_range.name,
                type=_tag_type_for_memory_type(memory_type),
                start=1,
                end=len(hardware_addresses),
                retentive=DEFAULT_RETENTIVE[memory_type],
            )

            mappings.append(logical_block.map_to(hardware_range))
            hardware_to_logical = {addr: i for i, addr in enumerate(hardware_addresses, start=1)}

            for row_idx in range(block_range.start_idx, block_range.end_idx + 1):
                row = rows[row_idx]
                if row.memory_type != memory_type:
                    continue
                logical_addr = hardware_to_logical.get(row.address)
                if logical_addr is None:
                    continue

                require_representable_block_nickname(
                    memory_type=row.memory_type,
                    address=row.address,
                    name=row.nickname,
                )
                slot_name = logical_block.slot_config(logical_addr).name
                if row.nickname != slot_name:
                    logical_block.rename_slot(logical_addr, row.nickname)
                slot_config = logical_block.slot_config(logical_addr)
                default = _parse_default(row.initial_value, logical_block.type)

                configure_kwargs: dict[str, object] = {}
                if row.retentive != slot_config.retentive:
                    configure_kwargs["retentive"] = row.retentive
                if default != slot_config.default:
                    configure_kwargs["default"] = default
                if configure_kwargs:
                    logical_block.configure_slot(logical_addr, **configure_kwargs)

        for idx, row in enumerate(rows):
            if idx in covered_rows:
                continue
            if row.nickname == "":
                continue

            existing = seen_names.get(row.nickname)
            if existing is not None and existing != (row.memory_type, row.address):
                existing_display = format_address_display(existing[0], existing[1])
                display = format_address_display(row.memory_type, row.address)
                raise ValueError(
                    f"Duplicate logical name {row.nickname!r} at {display}; already used at "
                    f"{existing_display}."
                )
            seen_names[row.nickname] = (row.memory_type, row.address)

            memory_type = row.memory_type
            logical_type = _tag_type_for_memory_type(memory_type)
            logical = Tag(
                name=row.nickname,
                type=logical_type,
                retentive=row.retentive,
                default=_parse_default(row.initial_value, logical_type),
            )
            hardware = _hardware_block_for(memory_type)[row.address]
            mappings.append(logical.map_to(hardware))

        return cls(mappings)

    def to_nickname_file(self, path: str | Path) -> int:
        """Write mapped addresses to a Click nickname CSV file.

        Emits one row per mapped hardware address.  Block entries produce
        rows with ``<Name>`` / ``</Name>`` comment markers that Click
        Programming Software can parse as block tag groups.  Unmapped
        addresses are omitted.

        Args:
            path: Destination CSV path. Parent directories must exist.

        Returns:
            Number of rows written.
        """
        records: dict[int, AddressRecord] = {}

        for entry in self._tag_entries_tuple:
            memory_type, address = self._parse_hardware_tag(entry.hardware)
            records[get_addr_key(memory_type, address)] = AddressRecord(
                memory_type=memory_type,
                address=address,
                nickname=entry.logical.name,
                comment="",
                initial_value=_format_default(entry.logical.default, entry.logical.type),
                retentive=entry.logical.retentive,
                data_type=BANKS[memory_type].data_type,
            )

        for entry in self._block_entries_tuple:
            if not entry.hardware_addresses:
                continue
            memory_type, _ = self._parse_hardware_tag(
                entry.hardware.block[entry.hardware_addresses[0]]
            )
            block_len = len(entry.hardware_addresses)

            for i, (logical_addr, hardware_addr) in enumerate(
                zip(entry.logical_addresses, entry.hardware_addresses, strict=True)
            ):
                slot = entry.logical[logical_addr]
                comment = ""
                if block_len == 1:
                    comment = format_block_tag(entry.logical.name, "self-closing")
                elif i == 0:
                    comment = format_block_tag(entry.logical.name, "open")
                elif i == block_len - 1:
                    comment = format_block_tag(entry.logical.name, "close")

                records[get_addr_key(memory_type, hardware_addr)] = AddressRecord(
                    memory_type=memory_type,
                    address=hardware_addr,
                    nickname=slot.name,
                    comment=comment,
                    initial_value=_format_default(slot.default, slot.type),
                    retentive=slot.retentive,
                    data_type=BANKS[memory_type].data_type,
                )

        return pyclickplc.write_csv(path, records)

    def resolve(self, source: Tag | Block | str, index: int | None = None) -> str:
        """Resolve a logical source to a hardware address string."""
        if isinstance(source, str):
            if index is not None:
                raise TypeError("Standalone tag resolution does not accept index.")
            entry = self._tag_forward.get(source)
            if entry is None:
                entry = self._system_tag_forward.get(source)
            if entry is None:
                entry = self._system_alias_forward.get(source)
            if entry is None:
                raise KeyError(f"No mapping for standalone tag {source!r}.")
            return entry.hardware.name

        if isinstance(source, Tag):
            if index is not None:
                raise TypeError("Standalone tag resolution does not accept index.")
            entry = self._tag_forward.get(source.name)
            if entry is None:
                entry = self._system_tag_forward.get(source.name)
            if entry is None:
                hardware = self._block_slot_forward_by_id.get(id(source))
                if hardware is not None:
                    return hardware.name
                hardware = self._block_slot_forward_by_name.get(source.name)
                if hardware is None:
                    raise KeyError(f"No mapping for standalone tag {source.name!r}.")
                return hardware.name
            return entry.hardware.name

        if isinstance(source, Block):
            if index is None:
                raise TypeError("Block resolution requires an index.")
            if not isinstance(index, int):
                raise TypeError("Block index must be int.")

            entry = self._block_lookup.get(id(source))
            if entry is None:
                raise KeyError(f"No mapping for block {source.name!r}.")
            hardware_addr = entry.logical_to_hardware.get(index)
            if hardware_addr is None:
                raise IndexError(f"Logical index {index} out of range for block {source.name!r}.")
            return entry.hardware.block[hardware_addr].name

        raise TypeError("resolve source must be Tag, Block, or str.")

    def offset_for(self, block: Block) -> int:
        """Return affine offset for a mapped block."""
        entry = self._block_lookup.get(id(block))
        if entry is None:
            raise KeyError(f"No mapping for block {block.name!r}.")
        if not entry.logical_addresses:
            raise ValueError(f"Block {block.name!r} has no mapped slots.")

        offsets = {
            hardware_addr - logical_addr
            for logical_addr, hardware_addr in zip(
                entry.logical_addresses, entry.hardware_addresses, strict=True
            )
        }
        if len(offsets) != 1:
            raise ValueError(f"Block {block.name!r} does not have an affine mapping.")
        return next(iter(offsets))

    def tags(self) -> tuple[_TagEntry, ...]:
        return self._tag_entries_tuple

    def blocks(self) -> tuple[_BlockEntry, ...]:
        return self._block_entries_tuple

    def block_entry_by_name(self, name: str) -> _BlockEntry | None:
        """Look up a block entry by logical block name."""
        return self._block_by_name.get(name)

    def validate(
        self,
        program: Program,
        mode: ValidationMode = "warn",
        profile: HardwareProfile | None = None,
    ) -> ClickValidationReport:
        """Validate a Program against Click portability rules.

        Args:
            program: The Program to validate.
            mode: "warn" (findings as hints) or "strict" (findings as errors).
            profile: Optional hardware capability profile override.

        Returns:
            ClickValidationReport with categorized findings.
        """
        from pyrung.click.validation import validate_click_program

        return validate_click_program(program, self, mode=mode, profile=profile)

    def mapped_slots(self) -> tuple[MappedSlot, ...]:
        """Return all mapped slots for runtime hardware-facing consumers."""
        slots: list[MappedSlot] = []

        for entry in self._entries_tuple:
            if isinstance(entry, _TagEntry):
                slots.append(
                    self._mapped_slot(entry.logical, entry.hardware, read_only=False, source="user")
                )
                continue

            for logical_addr, hardware_addr in zip(
                entry.logical_addresses, entry.hardware_addresses, strict=True
            ):
                logical_slot = entry.logical[logical_addr]
                hardware_slot = entry.hardware.block[hardware_addr]
                slots.append(
                    self._mapped_slot(logical_slot, hardware_slot, read_only=False, source="user")
                )

        for entry in self._system_tag_entries_tuple:
            read_only = self._system_read_only[entry.logical.name]
            slots.append(
                self._mapped_slot(
                    entry.logical, entry.hardware, read_only=read_only, source="system"
                )
            )

        return tuple(slots)

    @property
    def entries(self) -> tuple[_TagEntry | _BlockEntry, ...]:
        return self._entries_tuple

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    def __contains__(self, item: Tag | Block | str) -> bool:
        if isinstance(item, str):
            return (
                item in self._tag_forward
                or item in self._system_tag_forward
                or item in self._system_alias_forward
            )
        if isinstance(item, Tag):
            return (
                item.name in self._tag_forward
                or item.name in self._system_tag_forward
                or id(item) in self._block_slot_forward_by_id
                or item.name in self._block_slot_forward_by_name
            )
        if isinstance(item, Block):
            return id(item) in self._block_lookup
        return False

    def __len__(self) -> int:
        return len(self._entries_tuple)

    def __repr__(self) -> str:
        return (
            f"TagMap(tags={len(self._tag_entries_tuple)}, blocks={len(self._block_entries_tuple)})"
        )

    @staticmethod
    def _normalize_mappings(
        mappings: dict[Tag | Block, Tag | BlockRange] | Iterable[MappingEntry] | None,
    ) -> list[MappingEntry]:
        if mappings is None:
            return []
        if isinstance(mappings, dict):
            mapping_dict = cast(dict[Tag | Block, Tag | BlockRange], mappings)
            return [
                MappingEntry(source=source, target=target)
                for source, target in mapping_dict.items()
            ]

        normalized: list[MappingEntry] = []
        for item in mappings:
            if not isinstance(item, MappingEntry):
                raise TypeError("Iterable mappings must contain MappingEntry values.")
            normalized.append(item)
        return normalized

    def _build_block_entry(self, logical: Block, hardware: BlockRange) -> _BlockEntry:
        logical_addresses = tuple(logical.select(logical.start, logical.end).addresses)
        hardware_addresses = tuple(hardware.addresses)

        if len(logical_addresses) > len(hardware_addresses):
            return _BlockEntry(
                logical=logical,
                hardware=hardware,
                logical_addresses=logical_addresses,
                hardware_addresses=hardware_addresses,
                logical_to_hardware={},
            )

        aligned_hardware = hardware_addresses[: len(logical_addresses)]
        logical_to_hardware = dict(zip(logical_addresses, aligned_hardware, strict=True))
        return _BlockEntry(
            logical=logical,
            hardware=hardware,
            logical_addresses=logical_addresses,
            hardware_addresses=aligned_hardware,
            logical_to_hardware=logical_to_hardware,
        )

    @staticmethod
    def _parse_hardware_tag(tag: Tag) -> tuple[str, int]:
        try:
            memory_type, address = parse_address(tag.name)
        except ValueError as exc:
            raise ValueError(
                f"Hardware tag name {tag.name!r} is not a valid Click address."
            ) from exc
        return memory_type, address

    @classmethod
    def _validate_tag_mapping(cls, logical: Tag, hardware: Tag) -> None:
        memory_type, _ = cls._parse_hardware_tag(hardware)
        expected_type = _tag_type_for_memory_type(memory_type)
        if logical.type != expected_type:
            raise ValueError(
                f"Type mismatch for tag {logical.name!r}: logical {logical.type.name} "
                f"cannot map to {memory_type} ({expected_type.name})."
            )

    @classmethod
    def _validate_block_mapping(cls, entry: _BlockEntry) -> None:
        if not entry.hardware_addresses:
            raise ValueError(
                f"Block size mismatch: hardware range for {entry.logical.name!r} has no valid addresses."
            )
        sample_tag = entry.hardware.block[entry.hardware_addresses[0]]
        memory_type, _ = cls._parse_hardware_tag(sample_tag)
        expected_type = _tag_type_for_memory_type(memory_type)
        if entry.logical.type != expected_type:
            raise ValueError(
                f"Type mismatch for block {entry.logical.name!r}: logical {entry.logical.type.name} "
                f"cannot map to {memory_type} ({expected_type.name})."
            )

    @staticmethod
    def _claim_hardware_address(
        addr_key: int,
        *,
        owner: str,
        logical_name: str,
        used_hardware: dict[int, str],
        used_hardware_logical: dict[int, str],
        compatible_logical_names: set[str] | None = None,
    ) -> None:
        existing = used_hardware.get(addr_key)
        if existing is not None:
            if compatible_logical_names is not None:
                existing_logical = used_hardware_logical[addr_key]
                if existing_logical in compatible_logical_names:
                    return
            raise ValueError(f"Hardware address conflict between {existing} and {owner}.")
        used_hardware[addr_key] = owner
        used_hardware_logical[addr_key] = logical_name

    def _freeze_entries(self) -> None:
        self._tag_entries_tuple = tuple(self._tag_entries)
        self._block_entries_tuple = tuple(self._block_entries)
        self._entries_tuple = tuple(self._entries)
        self._system_tag_entries_tuple = tuple(self._system_tag_entries)
        del self._tag_entries
        del self._block_entries
        del self._entries
        del self._system_tag_entries

    def _mapped_slot(
        self,
        logical_slot: Tag,
        hardware_slot: Tag,
        *,
        read_only: bool,
        source: Literal["user", "system"],
    ) -> MappedSlot:
        memory_type, address = self._parse_hardware_tag(hardware_slot)
        return MappedSlot(
            hardware_address=format_address_display(memory_type, address),
            logical_name=logical_slot.name,
            default=logical_slot.default,
            memory_type=memory_type,
            address=address,
            read_only=read_only,
            source=source,
        )

    def _reject_reserved_system_name(self, name: str) -> None:
        if name in SYSTEM_TAGS_BY_NAME:
            raise ValueError(f"Logical tag name {name!r} is reserved for system points.")

    def _register_user_logical_name(self, name: str, *, owner: str) -> None:
        if name in self._user_logical_names:
            raise ValueError(f"Duplicate user logical tag name {name!r} (from {owner}).")
        self._user_logical_names.add(name)

    def _iter_export_slots(self) -> Iterable[Tag]:
        for entry in self._tag_entries_tuple:
            yield entry.logical
        for entry in self._block_entries_tuple:
            for logical_addr in entry.logical_addresses:
                yield entry.logical[logical_addr]

    def _refresh_nickname_validation(self) -> None:
        warnings: list[str] = []
        seen: dict[str, Tag] = {}

        for slot in self._iter_export_slots():
            nickname = slot.name
            if nickname != "":
                existing = seen.get(nickname)
                if existing is not None and existing is not slot:
                    raise ValueError(
                        f"Effective nickname collision: {nickname!r} is used by "
                        f"{existing.name!r} and {slot.name!r}."
                    )
                seen[nickname] = slot

            is_valid, error = validate_nickname(nickname)
            if not is_valid:
                warnings.append(f"Nickname {nickname!r} is invalid: {error}.")

        self._warnings = tuple(warnings)
