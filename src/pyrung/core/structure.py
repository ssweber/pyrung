"""Decorator-based structured logical tag factories.

`udt` creates mixed-type, field-grouped structures.
`named_array` creates single-type, instance-interleaved structures.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Sized
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar, Protocol, get_origin

from pyrung.core.memory_block import Block, BlockRange
from pyrung.core.physical import Physical
from pyrung.core.tag import (
    ChoiceMap,
    LiveTag,
    MappingEntry,
    Tag,
    TagType,
    _normalize_choices,
    _TagTypeBase,
)

UNSET = object()
_NUMERIC_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD})
_RESERVED_FIELD_NAMES = frozenset(
    {
        "clone",
        "count",
        "field_names",
        "fields",
        "map_to",
        "name",
        "instance",
        "instance_select",
        "stride",
        "type",
    }
)
_PRIMITIVE_TYPE_MAP = {
    bool: TagType.BOOL,
    int: TagType.INT,
    float: TagType.REAL,
    str: TagType.CHAR,
}
_TYPE_DEFAULT_RETENTIVE: dict[TagType, bool] = {
    TagType.BOOL: False,
    TagType.INT: True,
    TagType.DINT: True,
    TagType.REAL: True,
    TagType.WORD: True,
    TagType.CHAR: True,
}
_STRING_TYPE_MAP = {
    "bool": TagType.BOOL,
    "bit": TagType.BOOL,
    "int": TagType.INT,
    "dint": TagType.DINT,
    "int2": TagType.DINT,
    "real": TagType.REAL,
    "float": TagType.REAL,
    "word": TagType.WORD,
    "hex": TagType.WORD,
    "char": TagType.CHAR,
    "str": TagType.CHAR,
    "txt": TagType.CHAR,
}


@dataclass(frozen=True)
class Field:
    """Field metadata used by `udt` and `named_array` declarations.

    When ``retentive`` is ``None`` (the default), the field inherits
    its retentive policy from the resolved tag type (e.g. ``Int`` →
    ``True``, ``Bool`` → ``False``).  Pass an explicit ``bool`` only
    when you need to override the type default.
    """

    type: TagType | None = None
    default: Any = UNSET
    retentive: bool | None = None
    choices: ChoiceMap | None = None
    readonly: bool | None = None
    external: bool | None = None
    final: bool | None = None
    public: bool | None = None
    physical: Physical | None = None
    link: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    uom: str | None = None

    def __new__(
        cls,
        type: TagType | None = None,
        default: Any = UNSET,
        retentive: bool | None = None,
        choices: builtins.type[IntEnum] | ChoiceMap | None = None,
        readonly: bool | None = None,
        external: bool | None = None,
        final: bool | None = None,
        public: bool | None = None,
        physical: Physical | None = None,
        link: str | None = None,
        min: int | float | None = None,
        max: int | float | None = None,
        uom: str | None = None,
    ) -> Any:
        _ = (type, default, retentive, choices, readonly, external, final, public,
             physical, link, min, max, uom)
        return super().__new__(cls)


@dataclass(frozen=True)
class AutoDefault:
    """Descriptor for per-instance numeric default sequences."""

    start: int = 1
    step: int = 1


@dataclass(frozen=True)
class _FieldSpec:
    name: str
    type: TagType
    default: object
    retentive: bool
    choices: ChoiceMap | None = None
    readonly: bool = False
    external: bool = False
    final: bool = False
    public: bool = False
    physical: Physical | None = None
    link: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    uom: str | None = None


def auto(*, start: int = 1, step: int = 1) -> Any:
    """Create a per-instance numeric default sequence descriptor."""
    return AutoDefault(start=start, step=step)


def resolve_default(spec: object, index: int) -> object:
    """Resolve a field default specification for a 1-based index."""
    if isinstance(spec, AutoDefault):
        return spec.start + (index - 1) * spec.step
    if spec is UNSET:
        return None
    return spec


class DoneAccUDT(Protocol):
    """Any structured type with ``Done`` (Bool) and ``Acc`` (Int/Dint) fields.

    Satisfied by ``Timer[1]``, ``Counter[1]``, ``Timer.clone("Name")``,
    ``Counter.clone("Name")``, and any ``@udt()`` with matching fields.
    """

    Done: Tag
    Acc: Tag


class InstanceView:
    """1-based indexed view into one structure instance."""

    def __init__(self, owner: _StructRuntime, index: int):
        self._owner = owner
        self._index = index
        self._tag_cache: dict[str, Tag] = {}

    def __getattr__(self, field_name: str) -> Tag:
        cached = self._tag_cache.get(field_name)
        if cached is not None:
            return cached
        block = self._owner._blocks.get(field_name)
        if block is None:
            raise AttributeError(f"{type(self._owner).__name__!s} has no field {field_name!r}.")
        tag = block[self._index]
        self._tag_cache[field_name] = tag
        return tag

    def __repr__(self) -> str:
        return f"InstanceView({self._owner.name}[{self._index}])"


class _SelectedTagRange(BlockRange):
    """BlockRange-like wrapper backed by an explicit ordered tag list."""

    _selected_tags: tuple[LiveTag, ...]
    _label: str
    _instance_start: int
    _instance_end: int

    def __init__(
        self,
        block: Block,
        start: int,
        end: int,
        tags: tuple[LiveTag, ...],
        *,
        reverse_order: bool = False,
        label: str,
        instance_start: int,
        instance_end: int,
    ):
        super().__init__(block=block, start=start, end=end, reverse_order=reverse_order)
        object.__setattr__(self, "_selected_tags", tags)
        object.__setattr__(self, "_label", label)
        object.__setattr__(self, "_instance_start", instance_start)
        object.__setattr__(self, "_instance_end", instance_end)

    @property
    def addresses(self) -> range:
        if not self.reverse_order:
            return range(self.start, self.end + 1)
        return range(self.end, self.start - 1, -1)

    def tags(self) -> list[Tag]:
        result: list[Tag] = list(self._selected_tags)
        if self.reverse_order:
            result.reverse()
        return result

    def reverse(self) -> _SelectedTagRange:
        return _SelectedTagRange(
            self.block,
            self.start,
            self.end,
            self._selected_tags,
            reverse_order=not self.reverse_order,
            label=self._label,
            instance_start=self._instance_start,
            instance_end=self._instance_end,
        )

    def __len__(self) -> int:
        return len(self._selected_tags)

    def __iter__(self):
        yield from self.tags()

    def __repr__(self) -> str:
        if self._instance_start == self._instance_end:
            return f"{self._label}.instance({self._instance_start})"
        return f"{self._label}.instance_select({self._instance_start}, {self._instance_end})"


class _StructRuntime:
    """Runtime object returned by `@udt`."""

    def __init__(
        self,
        name: str,
        count: int,
        field_specs: tuple[_FieldSpec, ...],
        *,
        always_number: bool = False,
        readonly: bool = False,
        external: bool = False,
        final: bool = False,
        public: bool = False,
        kind: str = "udt",
    ):
        _validate_name(name)
        _validate_count(count)
        _validate_fields_present(field_specs)

        self.name = name
        self.count = count
        self.always_number = always_number
        self.readonly = bool(readonly)
        self.external = bool(external)
        self.final = bool(final)
        self.public = bool(public)
        self._structure_kind = kind
        self._original_field_specs = field_specs
        self._field_specs: dict[str, _FieldSpec] = {}
        self._field_order: tuple[str, ...] = tuple(spec.name for spec in field_specs)
        self._blocks: dict[str, Block] = {}

        for field_spec in field_specs:
            self._field_specs[field_spec.name] = field_spec
            block = Block(
                name=f"{name}.{field_spec.name}",
                type=field_spec.type,
                start=1,
                end=self.count,
                retentive=field_spec.retentive,
                address_formatter=(
                    _make_compact_formatter(name, field_spec.name)
                    if self.count == 1 and not self.always_number
                    else _make_formatter(name, field_spec.name)
                ),
                default_factory=_make_default_factory(field_spec.default),
            )
            block._pyrung_structure_runtime = self  # ty: ignore[unresolved-attribute]
            block._pyrung_structure_kind = self._structure_kind  # ty: ignore[unresolved-attribute]
            block._pyrung_structure_name = name  # ty: ignore[unresolved-attribute]
            block._pyrung_structure_field = field_spec.name  # ty: ignore[unresolved-attribute]
            block._pyrung_field_choices = field_spec.choices  # ty: ignore[unresolved-attribute]
            block._pyrung_field_readonly = field_spec.readonly  # ty: ignore[unresolved-attribute]
            block._pyrung_field_external = field_spec.external  # ty: ignore[unresolved-attribute]
            block._pyrung_field_final = field_spec.final  # ty: ignore[unresolved-attribute]
            block._pyrung_field_public = field_spec.public  # ty: ignore[unresolved-attribute]
            block._pyrung_field_physical = field_spec.physical  # ty: ignore[unresolved-attribute]
            block._pyrung_field_link = field_spec.link  # ty: ignore[unresolved-attribute]
            block._pyrung_field_min = field_spec.min  # ty: ignore[unresolved-attribute]
            block._pyrung_field_max = field_spec.max  # ty: ignore[unresolved-attribute]
            block._pyrung_field_uom = field_spec.uom  # ty: ignore[unresolved-attribute]
            self._blocks[field_spec.name] = block

    def clone(
        self,
        name: str,
        *,
        count: int | None = None,
        readonly: bool | None = None,
        external: bool | None = None,
        final: bool | None = None,
        public: bool | None = None,
    ) -> _StructRuntime:
        """Create a copy of this structure with a different base name."""
        return _StructRuntime(
            name=name,
            count=self.count if count is None else count,
            field_specs=self._original_field_specs,
            always_number=self.always_number,
            readonly=self.readonly if readonly is None else readonly,
            external=self.external if external is None else external,
            final=self.final if final is None else final,
            public=self.public if public is None else public,
            kind=self._structure_kind,
        )

    @property
    def fields(self) -> dict[str, Field]:
        return {
            name: Field(
                type=spec.type,
                default=spec.default,
                retentive=spec.retentive,
                choices=spec.choices,
                readonly=spec.readonly,
                external=spec.external,
                final=spec.final,
                public=spec.public,
            )
            for name, spec in self._field_specs.items()
        }

    @property
    def field_names(self) -> tuple[str, ...]:
        return self._field_order

    def __getitem__(self, index: int) -> InstanceView:
        if not isinstance(index, int):
            raise TypeError(f"{type(self).__name__} index must be an int.")
        if index < 1 or index > self.count:
            raise IndexError(f"{type(self).__name__} index {index} out of range 1..{self.count}.")
        return InstanceView(self, index)

    def __getattr__(self, field_name: str) -> Block | LiveTag:
        block = self._blocks.get(field_name)
        if block is None:
            raise AttributeError(f"{type(self).__name__} has no field {field_name!r}.")
        if self.count == 1:
            return block[1]
        return block

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({self.name!r}, count={self.count}, "
            f"fields={self._field_order!r})"
        )


class _NamedArrayRuntime(_StructRuntime):
    """Runtime object returned by `@named_array`."""

    def __init__(
        self,
        name: str,
        type: TagType,
        *,
        count: int,
        stride: int,
        field_specs: tuple[_FieldSpec, ...],
        always_number: bool = False,
        readonly: bool = False,
        external: bool = False,
        final: bool = False,
        public: bool = False,
    ):
        _validate_stride(stride)
        if stride < len(field_specs):
            raise ValueError(
                f"stride must be >= declared field count ({len(field_specs)}), got {stride}."
            )

        self.type = type
        self.stride = stride
        self._instance_block = Block(name=name, type=type, start=1, end=count * stride)
        super().__init__(
            name=name,
            count=count,
            field_specs=field_specs,
            always_number=always_number,
            readonly=readonly,
            external=external,
            final=final,
            public=public,
            kind="named_array",
        )

    def clone(
        self,
        name: str,
        *,
        count: int | None = None,
        stride: int | None = None,
        readonly: bool | None = None,
        external: bool | None = None,
        final: bool | None = None,
        public: bool | None = None,
    ) -> _NamedArrayRuntime:
        """Create a copy of this named array with a different base name."""
        return _NamedArrayRuntime(
            name=name,
            type=self.type,
            count=self.count if count is None else count,
            stride=self.stride if stride is None else stride,
            field_specs=self._original_field_specs,
            always_number=self.always_number,
            readonly=self.readonly if readonly is None else readonly,
            external=self.external if external is None else external,
            final=self.final if final is None else final,
            public=self.public if public is None else public,
        )

    def hardware_span(self, hw_start: int) -> tuple[int, int]:
        """Return (start, end) for an inclusive hardware select range."""
        return (hw_start, hw_start + self.count * self.stride - 1)

    def map_to(self, target: BlockRange) -> list[MappingEntry]:
        """Map this named-array layout to a hardware range."""
        if not isinstance(target, BlockRange):
            raise TypeError("named_array.map_to target must be a BlockRange.")

        hardware_addresses = tuple(target.addresses)
        expected = self.count * self.stride
        if len(hardware_addresses) != expected:
            raise ValueError(
                f"named_array {self.name!r} expects {expected} hardware slots, "
                f"received {len(hardware_addresses)}."
            )

        if len(self._field_order) == 1 and self.stride == 1:
            field_name = self._field_order[0]
            return [self._blocks[field_name].map_to(target)]

        entries: list[MappingEntry] = []
        for index in range(1, self.count + 1):
            base = (index - 1) * self.stride
            for offset, field_name in enumerate(self._field_order):
                logical = self._blocks[field_name][index]
                hardware = target.block[hardware_addresses[base + offset]]
                entries.append(logical.map_to(hardware))
        return entries

    def instance(self, index: int) -> BlockRange:
        """Select a single named-array instance as a contiguous BlockRange."""
        return self._instance_range(index, index)

    def instance_select(self, start: int, end: int) -> BlockRange:
        """Select a range of named-array instances as a contiguous BlockRange."""
        return self._instance_range(start, end)

    def _instance_range(self, start: int, end: int) -> BlockRange:
        if not isinstance(start, int) or not isinstance(end, int):
            raise TypeError("instance bounds must be integers.")
        if start < 1 or end < 1 or start > self.count or end > self.count:
            raise IndexError(f"instance bounds {start}..{end} out of range 1..{self.count}.")
        if start > end:
            raise ValueError(f"instance start ({start}) must be <= end ({end}) for {self.name}.")

        tags: list[LiveTag] = []
        for inst in range(start, end + 1):
            for field_name in self._field_order:
                tags.append(self._blocks[field_name][inst])

        raw_start = (start - 1) * self.stride + 1
        raw_end = end * self.stride
        return _SelectedTagRange(
            self._instance_block,
            raw_start,
            raw_end,
            tuple(tags),
            label=self.name,
            instance_start=start,
            instance_end=end,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({self.name!r}, {self.type}, count={self.count}, "
            f"stride={self.stride}, fields={self._field_order!r})"
        )


def udt(
    *,
    count: int = 1,
    always_number: bool = False,
    readonly: bool = False,
    external: bool = False,
    final: bool = False,
    public: bool = False,
) -> Callable[[type[Any]], _StructRuntime]:
    """Decorator that builds a mixed-type structured runtime from annotations."""
    _validate_count(count)

    def _decorator(cls: type[Any]) -> _StructRuntime:
        name = cls.__name__
        _validate_name(name)
        field_specs = _parse_udt_fields(
            cls, readonly=readonly, external=external, final=final, public=public
        )
        return _StructRuntime(
            name=name,
            count=count,
            field_specs=field_specs,
            always_number=always_number,
            readonly=readonly,
            external=external,
            final=final,
            public=public,
        )

    return _decorator


def named_array(
    base_type: object,
    *,
    count: int = 1,
    stride: int = 1,
    always_number: bool = False,
    readonly: bool = False,
    external: bool = False,
    final: bool = False,
    public: bool = False,
) -> Callable[[type[Any]], _NamedArrayRuntime]:
    """Decorator that builds a single-type, instance-interleaved structured runtime."""
    _validate_count(count)
    _validate_stride(stride)
    resolved_type = _resolve_annotation(base_type, "base_type")

    def _decorator(cls: type[Any]) -> _NamedArrayRuntime:
        name = cls.__name__
        _validate_name(name)
        field_specs = _parse_named_array_fields(
            cls, resolved_type, readonly=readonly, external=external, final=final, public=public
        )
        return _NamedArrayRuntime(
            name=name,
            type=resolved_type,
            count=count,
            stride=stride,
            field_specs=field_specs,
            always_number=always_number,
            readonly=readonly,
            external=external,
            final=final,
            public=public,
        )

    return _decorator


def _parse_udt_fields(
    cls: type[Any],
    *,
    readonly: bool = False,
    external: bool = False,
    final: bool = False,
    public: bool = False,
) -> tuple[_FieldSpec, ...]:
    annotations = getattr(cls, "__annotations__", {})
    if not isinstance(annotations, dict):
        raise TypeError("UDT annotations must be a dict.")

    classvar_names = {
        field_name
        for field_name, annotation in annotations.items()
        if _is_classvar_annotation(annotation)
    }
    field_annotations = {
        field_name: annotation
        for field_name, annotation in annotations.items()
        if field_name not in classvar_names
    }

    _validate_fields_present(field_annotations)
    _validate_field_names(field_annotations.keys())

    parsed: list[_FieldSpec] = []
    for field_name, annotation in field_annotations.items():
        field_type = _resolve_annotation(annotation, field_name)
        raw_default = cls.__dict__.get(field_name, UNSET)
        parsed.append(
            _build_field_spec(
                field_name,
                field_type,
                raw_default,
                source="udt",
                readonly=readonly,
                external=external,
                final=final,
                public=public,
            )
        )
    return tuple(parsed)


def _parse_named_array_fields(
    cls: type[Any],
    base_type: TagType,
    *,
    readonly: bool = False,
    external: bool = False,
    final: bool = False,
    public: bool = False,
) -> tuple[_FieldSpec, ...]:
    annotations = getattr(cls, "__annotations__", {})
    classvar_names: set[str] = set()
    if isinstance(annotations, dict):
        classvar_names = {
            field_name
            for field_name, annotation in annotations.items()
            if _is_classvar_annotation(annotation)
        }

    parsed: list[_FieldSpec] = []
    for field_name, value in cls.__dict__.items():
        if _should_skip_named_array_attr(field_name, value, classvar_names=classvar_names):
            continue
        parsed.append(
            _build_field_spec(
                field_name,
                base_type,
                value,
                source="named_array",
                readonly=readonly,
                external=external,
                final=final,
                public=public,
            )
        )

    _validate_fields_present(parsed)
    _validate_field_names(spec.name for spec in parsed)
    return tuple(parsed)


def _is_classvar_annotation(annotation: object) -> bool:
    origin = get_origin(annotation)
    if origin is ClassVar or annotation is ClassVar:
        return True

    if isinstance(annotation, str):
        normalized = annotation.replace(" ", "")
        return (
            normalized == "ClassVar"
            or normalized.startswith("ClassVar[")
            or normalized == "typing.ClassVar"
            or normalized.startswith("typing.ClassVar[")
        )

    return False


def _should_skip_named_array_attr(name: str, value: object, *, classvar_names: set[str]) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return True
    if name in classvar_names:
        return True
    if isinstance(value, (classmethod, staticmethod, property)):
        return True
    return callable(value)


def _build_field_spec(
    field_name: str,
    type: TagType,
    raw_default: object,
    *,
    source: str,
    readonly: bool = False,
    external: bool = False,
    final: bool = False,
    public: bool = False,
) -> _FieldSpec:
    retentive: bool | None = None
    default_spec = raw_default
    choices: ChoiceMap | None = None
    field_readonly = bool(readonly)
    field_external = bool(external)
    field_final = bool(final)
    field_public = bool(public)

    field_physical: Physical | None = None
    field_link: str | None = None
    field_min: int | float | None = None
    field_max: int | float | None = None
    field_uom: str | None = None

    if isinstance(raw_default, Field):
        if raw_default.type is not None and raw_default.type != type:
            if source == "named_array":
                raise ValueError(
                    f"named_array field {field_name!r} cannot declare type; "
                    "use the decorator base_type."
                )
            raise ValueError(
                f"udt field {field_name!r} type mismatch: annotation resolves to {type.name}, "
                f"but Field.type is {raw_default.type.name}."
            )
        retentive = raw_default.retentive
        default_spec = raw_default.default
        choices = _normalize_choices(
            raw_default.choices,
            tag_type=type,
            owner=f"Field({field_name!r}) choices",
        )
        if raw_default.readonly is not None:
            field_readonly = bool(raw_default.readonly)
        if raw_default.external is not None:
            field_external = bool(raw_default.external)
        if raw_default.final is not None:
            field_final = bool(raw_default.final)
        if raw_default.public is not None:
            field_public = bool(raw_default.public)
        field_physical = raw_default.physical
        field_link = raw_default.link
        field_min = raw_default.min
        field_max = raw_default.max
        field_uom = raw_default.uom

    if retentive is None:
        retentive = _TYPE_DEFAULT_RETENTIVE[type]

    _validate_auto_default_allowed(field_name, default_spec, type)
    return _FieldSpec(
        name=field_name,
        type=type,
        default=default_spec,
        retentive=retentive,
        choices=choices,
        readonly=field_readonly,
        external=field_external,
        final=field_final,
        public=field_public,
        physical=field_physical,
        link=field_link,
        min=field_min,
        max=field_max,
        uom=field_uom,
    )


def _resolve_annotation(annotation: object, field_name: str) -> TagType:
    """Resolve an annotation/base type to TagType."""
    if isinstance(annotation, TagType):
        return annotation

    if isinstance(annotation, str):
        token = annotation.strip().split(".")[-1].strip().strip("'\"")
        resolved = _STRING_TYPE_MAP.get(token.lower())
        if resolved is not None:
            return resolved
        raise TypeError(
            f"Field {field_name!r} annotation {annotation!r} is not supported. "
            "Use Bool/Int/Dint/Real/Word/Char, bool/int/float/str, or IEC names."
        )

    if isinstance(annotation, type) and issubclass(annotation, _TagTypeBase):
        return annotation._tag_type

    primitive = _PRIMITIVE_TYPE_MAP.get(annotation)  # ty: ignore[invalid-argument-type]
    if primitive is not None:
        return primitive

    raise TypeError(
        f"Field {field_name!r} annotation {annotation!r} is not supported. "
        "Use Bool/Int/Dint/Real/Word/Char, bool/int/float/str, or IEC names."
    )


def _make_default_factory(default_spec: object):
    def _factory(index: int) -> object:
        return resolve_default(default_spec, index)

    return _factory


def _make_formatter(struct_name: str, field_name: str):
    def _formatter(_: str, addr: int) -> str:
        return f"{struct_name}{addr}_{field_name}"

    return _formatter


def _make_compact_formatter(struct_name: str, field_name: str):
    def _formatter(_: str, __: int) -> str:
        return f"{struct_name}_{field_name}"

    return _formatter


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or name.strip() == "":
        raise ValueError("Struct name must be a non-empty string.")


def _validate_count(count: int) -> None:
    if not isinstance(count, int) or count < 1:
        raise ValueError(f"count must be an int >= 1, got {count!r}.")


def _validate_stride(stride: int) -> None:
    if not isinstance(stride, int) or stride < 1:
        raise ValueError(f"stride must be an int >= 1, got {stride!r}.")


def _validate_fields_present(fields: Sized) -> None:
    if len(fields) == 0:
        raise ValueError("At least one field is required.")


def _validate_field_names(field_names: Iterable[str]) -> None:
    for field_name in field_names:
        if field_name in _RESERVED_FIELD_NAMES:
            raise ValueError(f"Field name {field_name!r} is reserved.")


def _validate_auto_default_allowed(field_name: str, default: object, type: TagType) -> None:
    if isinstance(default, AutoDefault) and type not in _NUMERIC_TYPES:
        raise ValueError(
            f"Field {field_name!r} uses auto() but type {type.name} is not numeric. "
            "Supported types: INT, DINT, WORD."
        )


# ---------------------------------------------------------------------------
# Built-in Timer / Counter UDTs
# ---------------------------------------------------------------------------
# Built-in Timer / Counter UDTs (count=1).  Use ``Timer.clone("Name")``
# for named instances.  For a full Click-sized pool, clone with an
# explicit count: ``Timer.clone("T", count=500)``.


Timer = _StructRuntime(
    name="Timer",
    count=1,
    field_specs=(
        _FieldSpec("Done", TagType.BOOL, UNSET, retentive=False),
        _FieldSpec("Acc", TagType.INT, UNSET, retentive=True),
    ),
)

Counter = _StructRuntime(
    name="Counter",
    count=1,
    field_specs=(
        _FieldSpec("Done", TagType.BOOL, UNSET, retentive=False),
        _FieldSpec("Acc", TagType.DINT, UNSET, retentive=True),
    ),
)
