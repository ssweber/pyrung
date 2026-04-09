"""Automatically generated module split."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pyclickplc.addresses import (
    format_address_display,
    get_addr_key,
    parse_address,
)
from pyclickplc.validation import validate_nickname

from pyrung.click.system_mappings import SYSTEM_CLICK_SLOTS
from pyrung.core import Block, BlockRange, Tag
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import MappingEntry

from ._nickname_io import tag_map_from_nickname_file, write_tag_map_to_nickname_file
from ._parsers import (
    _tag_type_for_memory_type,
)
from ._types import (
    MappedSlot,
    OwnerInfo,
    StructuredImport,
    _BlockEntry,
    _TagEntry,
)

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile
    from pyrung.click.validation import ClickValidationReport, ValidationMode
    from pyrung.core.program import Program

_RESERVED_SYSTEM_HARDWARE_KEYS: frozenset[int] = frozenset(
    get_addr_key(*parse_address(slot.hardware.name)) for slot in SYSTEM_CLICK_SLOTS
)

_BLOCK_SLOT_OWNER_RE = re.compile(r"^block slot (?P<block_name>.+)\[(?P<addr>[0-9]+)\]$")


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
        self._user_logical_name_owners: dict[str, str] = {}
        self._block_slot_forward_by_id: dict[int, Tag] = {}
        self._block_slot_forward_by_name: dict[str, Tag] = {}
        self._warnings: tuple[str, ...] = ()
        self._structures: tuple[StructuredImport, ...] = ()
        self._structure_by_name: dict[str, StructuredImport] = {}
        self._structure_warnings: tuple[str, ...] = ()
        self._named_array_spans: dict[str, tuple[str, int, int]] = {}

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

        self._populate_programmatic_structures()
        self._freeze_entries()
        self._refresh_nickname_validation()

    @classmethod
    def from_nickname_file(
        cls,
        path: str | Path,
        *,
        mode: Literal["warn", "strict"] = "warn",
    ) -> TagMap:
        """Build a `TagMap` from a Click nickname CSV file."""
        return tag_map_from_nickname_file(
            cls,
            path,
            mode=mode,
            reserved_system_hardware_keys=_RESERVED_SYSTEM_HARDWARE_KEYS,
        )

    def to_nickname_file(self, path: str | Path) -> int:
        """Write this mapping to a Click nickname CSV file."""
        return write_tag_map_to_nickname_file(self, path)

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

    def _offset_for(self, block: Block) -> int:
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

    def _block_entry_by_name(self, name: str) -> _BlockEntry | None:
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

    def tags_from_plc_data(
        self,
        data: Mapping[str, bool | int | float | str],
    ) -> dict[str, bool | int | float | str]:
        """Return logical tag values from a PLC data dump.

        Accepts a dict keyed by Click display addresses (e.g. from
        ``pyclickplc.read_plc_data``) and returns a dict keyed by the
        logical tag names in this mapping.  Unmapped addresses are
        silently skipped.

        .. code-block:: python

            from pyclickplc import read_plc_data

            data = read_plc_data("data.csv", skip_default=True)
            tags = mapping.tags_from_plc_data(data)
            runner = PLC(logic, initial_state=SystemState().with_tags(tags))

        Args:
            data: ``{hardware_address: value}`` dict — keys are normalised
                Click display addresses such as ``"X001"`` or ``"DS3"``.

        Returns:
            ``{logical_name: value}`` dict containing only the addresses
            that appear in both *data* and this TagMap.
        """
        reverse: dict[str, str] = {}
        for slot in self.mapped_slots():
            if slot.memory_type in ("XD", "YD"):
                continue
            reverse[slot.hardware_address] = slot.logical_name

        return {reverse[addr]: value for addr, value in data.items() if addr in reverse}

    @property
    def entries(self) -> tuple[_TagEntry | _BlockEntry, ...]:
        return self._entries_tuple

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    @property
    def structures(self) -> tuple[StructuredImport, ...]:
        return self._structures

    def structure_by_name(self, name: str) -> StructuredImport | None:
        return self._structure_by_name.get(name)

    @property
    def structure_warnings(self) -> tuple[str, ...]:
        return self._structure_warnings

    def _owner_of(self, display_address: str) -> OwnerInfo | None:
        """Return the structure/block that owns a hardware display address.

        Args:
            display_address: Click display address string (e.g. ``"DS103"``).

        Returns:
            An :class:`OwnerInfo` describing the owning structure, or ``None``
            if the address is not mapped.
        """
        if not hasattr(self, "_reverse_index"):
            self._build_reverse_index()
        return self._reverse_index.get(display_address)

    def _build_reverse_index(self) -> None:
        """Build the reverse index mapping display addresses to OwnerInfo."""
        index: dict[str, OwnerInfo] = {}

        # 1. Index plain block entries
        structure_block_names: set[str] = set()
        for structure in self._structures:
            runtime = cast(Any, structure.runtime)
            for field_name in runtime.field_names:
                structure_block_names.add(runtime._blocks[field_name].name)

        for block_entry in self._block_entries_tuple:
            if block_entry.logical.name in structure_block_names:
                continue
            for logical_addr, hardware_addr in zip(
                block_entry.logical_addresses,
                block_entry.hardware_addresses,
                strict=True,
            ):
                hw_tag = block_entry.hardware.block[hardware_addr]
                memory_type, address = self._parse_hardware_tag(hw_tag)
                display = format_address_display(memory_type, address)
                index[display] = OwnerInfo(
                    structure_name=block_entry.logical.name,
                    instance=logical_addr,
                    field=None,
                    structure_type="block",
                )

        # 2. Index structures (overwrites block entries for same addresses)
        for structure in self._structures:
            runtime = cast(Any, structure.runtime)
            for field_name in runtime.field_names:
                block = runtime._blocks[field_name]
                for i in range(1, structure.count + 1):
                    logical_slot = block[i]
                    # Try block slot forward (Block→BlockRange entries)
                    hw_tag = self._block_slot_forward_by_name.get(logical_slot.name)
                    if hw_tag is None:
                        hw_tag = self._block_slot_forward_by_id.get(id(logical_slot))
                    # Try tag forward (Tag→Tag entries, e.g. from named_array.map_to)
                    if hw_tag is None:
                        tag_entry = self._tag_forward.get(logical_slot.name)
                        if tag_entry is not None:
                            hw_tag = tag_entry.hardware
                    if hw_tag is None:
                        continue
                    memory_type, address = self._parse_hardware_tag(hw_tag)
                    display = format_address_display(memory_type, address)
                    index[display] = OwnerInfo(
                        structure_name=structure.name,
                        instance=i if structure.count > 1 else None,
                        field=field_name,
                        structure_type=structure.kind,
                    )

        self._reverse_index = index

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

    @staticmethod
    def _structure_provenance(logical: Tag | Block) -> tuple[str, str, Any] | None:
        runtime = getattr(logical, "_pyrung_structure_runtime", None)
        kind = getattr(logical, "_pyrung_structure_kind", None)
        name = getattr(logical, "_pyrung_structure_name", None)
        if runtime is None or kind not in {"udt", "named_array"} or not isinstance(name, str):
            return None
        return cast(str, kind), name, runtime

    def _populate_programmatic_structures(self) -> None:
        structures: dict[str, StructuredImport] = {}
        named_array_spans: dict[str, tuple[str, int, int]] = {}

        def register_structure(logical: Tag | Block) -> tuple[str, str, Any] | None:
            provenance = self._structure_provenance(logical)
            if provenance is None:
                return None

            kind, name, runtime = provenance
            existing = structures.get(name)
            stride = cast(int | None, getattr(runtime, "stride", None))
            if existing is None:
                structures[name] = StructuredImport(
                    name=name,
                    kind=cast(Literal["udt", "named_array"], kind),
                    runtime=runtime,
                    count=cast(int, runtime.count),
                    stride=stride,
                )
                return provenance

            if existing.kind != kind or existing.runtime is not runtime:
                raise ValueError(f"Duplicate structured mapping name {name!r}.")
            return provenance

        def record_named_array_span(logical: Tag | Block, memory_type: str, address: int) -> None:
            provenance = register_structure(logical)
            if provenance is None:
                return

            kind, name, runtime = provenance
            if kind != "named_array":
                return

            field_name = cast(str, logical._pyrung_structure_field)  # ty: ignore[unresolved-attribute]
            field_offset = cast(tuple[str, ...], runtime.field_names).index(field_name)
            stride = cast(int, runtime.stride)
            instance = (
                logical.start
                if isinstance(logical, Block)
                else cast(int, logical._pyrung_structure_index)  # ty: ignore[unresolved-attribute]
            )
            span_start = address - ((instance - 1) * stride + field_offset)
            span_end = span_start + cast(int, runtime.count) * stride - 1
            existing = named_array_spans.get(name)
            if existing is None:
                named_array_spans[name] = (memory_type, span_start, span_end)
                return
            existing_memory_type, start, end = existing
            if existing_memory_type != memory_type:
                raise ValueError(
                    f"Named array {name!r} spans multiple memory types: "
                    f"{existing_memory_type} and {memory_type}."
                )
            named_array_spans[name] = (
                memory_type,
                min(start, span_start),
                max(end, span_end),
            )

        for entry in self._block_entries:
            provenance = register_structure(entry.logical)
            if provenance is None:
                continue
            kind, _, _ = provenance
            if kind != "named_array":
                continue
            if not entry.hardware_addresses:
                continue
            sample_tag = entry.hardware.block[entry.hardware_addresses[0]]
            memory_type, address = self._parse_hardware_tag(sample_tag)
            record_named_array_span(entry.logical, memory_type, address)

        for entry in self._tag_entries:
            provenance = register_structure(entry.logical)
            if provenance is None:
                continue
            kind, _, _ = provenance
            if kind != "named_array":
                continue
            memory_type, address = self._parse_hardware_tag(entry.hardware)
            record_named_array_span(entry.logical, memory_type, address)

        self._structures = tuple(structures.values())
        self._structure_by_name = dict(structures)
        self._named_array_spans = named_array_spans

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

    def _duplicate_user_logical_name_hint(
        self, name: str, *, owner: str, existing_owner: str
    ) -> str:
        for candidate_owner in (owner, existing_owner):
            match = _BLOCK_SLOT_OWNER_RE.fullmatch(candidate_owner)
            if match is None:
                continue
            block_name = match.group("block_name")
            addr = int(match.group("addr"))
            if not block_name.endswith(tuple("0123456789")):
                continue
            if name != f"{block_name}{addr}":
                continue
            return (
                " Auto-generated block slot names concatenate the block tag and slot number, "
                f"so {block_name}[{addr}] becomes {name!r}. If a block tag ends in digits, "
                "that can collide with another block's later slot numbers. Consider renaming "
                "the block tag so it does not end in a number, or rename the slot explicitly."
            )
        return ""

    def _register_user_logical_name(self, name: str, *, owner: str) -> None:
        existing_owner = self._user_logical_name_owners.get(name)
        if existing_owner is not None:
            hint = self._duplicate_user_logical_name_hint(
                name, owner=owner, existing_owner=existing_owner
            )
            raise ValueError(
                f"Duplicate user logical tag name {name!r} (from {owner}; already used by "
                f"{existing_owner}).{hint}"
            )
        self._user_logical_name_owners[name] = owner

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
