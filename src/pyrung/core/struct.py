"""Structured logical tag factories.

Struct creates mixed-type, field-grouped blocks.
PackedStruct creates single-type, instance-grouped records with optional padding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.memory_block import Block, BlockRange
from pyrung.core.tag import MappingEntry, Tag, TagType

UNSET = object()
_NUMERIC_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD})
_RESERVED_FIELD_NAMES = frozenset(
    {
        "count",
        "field_names",
        "fields",
        "map_to",
        "name",
        "pad",
        "type",
        "width",
    }
)


@dataclass(frozen=True)
class Field:
    """Field descriptor used by Struct and PackedStruct."""

    type: TagType | None = None
    default: Any = UNSET
    retentive: bool = False


@dataclass(frozen=True)
class AutoDefault:
    """Descriptor for per-instance numeric default sequences."""

    start: int = 1
    step: int = 1


def auto(*, start: int = 1, step: int = 1) -> AutoDefault:
    """Create a per-instance numeric default sequence descriptor."""
    return AutoDefault(start=start, step=step)


def resolve_default(spec: object, index: int) -> object:
    """Resolve a field default specification for a 1-based index."""
    if isinstance(spec, AutoDefault):
        return spec.start + (index - 1) * spec.step
    if spec is UNSET:
        return None
    return spec


class InstanceView:
    """1-based indexed view into a Struct/PackedStruct instance."""

    def __init__(self, owner: Struct | PackedStruct, index: int):
        self._owner = owner
        self._index = index

    def __getattr__(self, field_name: str) -> Tag:
        block = self._owner._blocks.get(field_name)
        if block is None:
            raise AttributeError(f"{type(self._owner).__name__!s} has no field {field_name!r}.")
        return block[self._index]

    def __repr__(self) -> str:
        return f"InstanceView({self._owner.name}[{self._index}])"


class Struct:
    """Mixed-type logical record factory with field-grouped layout."""

    def __init__(self, name: str, *, count: int, **fields: Field):
        _validate_name(name)
        _validate_count(count)
        _validate_fields_present(fields)
        _validate_field_names(fields)

        self.name = name
        self.count = count
        self._field_specs: dict[str, Field] = {}
        self._field_order: tuple[str, ...] = tuple(fields.keys())
        self._blocks: dict[str, Block] = {}

        for field_name, field_spec in fields.items():
            if not isinstance(field_spec, Field):
                raise TypeError(f"Field {field_name!r} must be a Field descriptor.")
            if field_spec.type is None:
                raise ValueError(f"Struct field {field_name!r} requires a TagType.")
            _validate_auto_default_allowed(field_name, field_spec.default, field_spec.type)

            self._field_specs[field_name] = field_spec
            self._blocks[field_name] = Block(
                name=f"{name}.{field_name}",
                type=field_spec.type,
                start=1,
                end=count,
                retentive=field_spec.retentive,
                address_formatter=_make_formatter(name, field_name),
                default_factory=_make_default_factory(field_spec.default),
            )

    @property
    def fields(self) -> dict[str, Field]:
        return dict(self._field_specs)

    @property
    def field_names(self) -> tuple[str, ...]:
        return self._field_order

    def __getitem__(self, index: int) -> InstanceView:
        if not isinstance(index, int):
            raise TypeError("Struct index must be an int.")
        if index < 1 or index > self.count:
            raise IndexError(f"Struct index {index} out of range 1..{self.count}.")
        return InstanceView(self, index)

    def __getattr__(self, field_name: str) -> Block:
        block = self._blocks.get(field_name)
        if block is None:
            raise AttributeError(f"Struct has no field {field_name!r}.")
        return block

    def __repr__(self) -> str:
        return f"Struct({self.name!r}, count={self.count}, fields={self._field_order!r})"


class PackedStruct:
    """Single-type logical record factory with instance-grouped layout."""

    def __init__(self, name: str, type: TagType, *, count: int, pad: int = 0, **fields: Field):
        _validate_name(name)
        _validate_count(count)
        _validate_pad(pad)
        _validate_fields_present(fields)
        _validate_field_names(fields)

        self.name = name
        self.type = type
        self.count = count
        self.pad = pad
        self._field_specs: dict[str, Field] = {}
        self._field_order: tuple[str, ...]
        self._blocks: dict[str, Block] = {}

        for field_name, field_spec in fields.items():
            if not isinstance(field_spec, Field):
                raise TypeError(f"Field {field_name!r} must be a Field descriptor.")
            if field_spec.type is not None:
                raise ValueError(
                    f"PackedStruct field {field_name!r} cannot declare type; use PackedStruct type."
                )
            _validate_auto_default_allowed(field_name, field_spec.default, type)
            self._field_specs[field_name] = field_spec

        pad_names = tuple(f"empty{i}" for i in range(1, pad + 1))
        for pad_name in pad_names:
            if pad_name in fields:
                raise ValueError(f"Padding field name {pad_name!r} collides with a user field.")
            self._field_specs[pad_name] = Field()

        self._field_order = tuple(fields.keys()) + pad_names
        self.width = len(self._field_order)

        for field_name in self._field_order:
            field_spec = self._field_specs[field_name]
            self._blocks[field_name] = Block(
                name=f"{name}.{field_name}",
                type=type,
                start=1,
                end=count,
                retentive=field_spec.retentive,
                address_formatter=_make_formatter(name, field_name),
                default_factory=_make_default_factory(field_spec.default),
            )

    @property
    def fields(self) -> dict[str, Field]:
        return dict(self._field_specs)

    @property
    def field_names(self) -> tuple[str, ...]:
        return self._field_order

    def __getitem__(self, index: int) -> InstanceView:
        if not isinstance(index, int):
            raise TypeError("PackedStruct index must be an int.")
        if index < 1 or index > self.count:
            raise IndexError(f"PackedStruct index {index} out of range 1..{self.count}.")
        return InstanceView(self, index)

    def __getattr__(self, field_name: str) -> Block:
        block = self._blocks.get(field_name)
        if block is None:
            raise AttributeError(f"PackedStruct has no field {field_name!r}.")
        return block

    def map_to(self, target: BlockRange) -> list[MappingEntry]:
        """Map this packed layout to a hardware range."""
        if not isinstance(target, BlockRange):
            raise TypeError("PackedStruct.map_to target must be a BlockRange.")

        hardware_addresses = tuple(target.addresses)
        expected = self.count * self.width
        if len(hardware_addresses) != expected:
            raise ValueError(
                f"PackedStruct {self.name!r} expects {expected} hardware slots, "
                f"received {len(hardware_addresses)}."
            )

        if self.width == 1:
            field_name = self._field_order[0]
            return [self._blocks[field_name].map_to(target)]

        entries: list[MappingEntry] = []
        for index in range(1, self.count + 1):
            base = (index - 1) * self.width
            for offset, field_name in enumerate(self._field_order):
                logical = self._blocks[field_name][index]
                hardware = target.block[hardware_addresses[base + offset]]
                entries.append(logical.map_to(hardware))
        return entries

    def __repr__(self) -> str:
        return (
            f"PackedStruct({self.name!r}, {self.type}, count={self.count}, "
            f"pad={self.pad}, fields={self._field_order!r})"
        )


def _make_default_factory(default_spec: object):
    def _factory(index: int) -> object:
        return resolve_default(default_spec, index)

    return _factory


def _make_formatter(struct_name: str, field_name: str):
    def _formatter(_: str, addr: int) -> str:
        return f"{struct_name}{addr}_{field_name}"

    return _formatter


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or name.strip() == "":
        raise ValueError("Struct name must be a non-empty string.")


def _validate_count(count: int) -> None:
    if not isinstance(count, int) or count < 1:
        raise ValueError(f"count must be an int >= 1, got {count!r}.")


def _validate_pad(pad: int) -> None:
    if not isinstance(pad, int) or pad < 0:
        raise ValueError(f"pad must be an int >= 0, got {pad!r}.")


def _validate_fields_present(fields: dict[str, Field]) -> None:
    if len(fields) == 0:
        raise ValueError("At least one field is required.")


def _validate_field_names(fields: dict[str, Field]) -> None:
    for field_name in fields:
        if field_name in _RESERVED_FIELD_NAMES:
            raise ValueError(f"Field name {field_name!r} is reserved.")


def _validate_auto_default_allowed(field_name: str, default: object, type: TagType) -> None:
    if isinstance(default, AutoDefault) and type not in _NUMERIC_TYPES:
        raise ValueError(
            f"Field {field_name!r} uses auto() but type {type.name} is not numeric. "
            "Supported types: INT, DINT, WORD."
        )
